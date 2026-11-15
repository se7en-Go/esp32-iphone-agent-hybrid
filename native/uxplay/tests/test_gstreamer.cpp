/* Offline: synthetic video only. No RAOP server, sockets, mDNS, or phone. */
#include "../source/renderers/hafe_renderer.cpp"
#include <cassert>
#include <chrono>
#include <fstream>
#include <iostream>
#include <thread>
#include <vector>
#include <unistd.h>

static uint64_t read_be(const unsigned char *p,int n) { uint64_t v=0;while(n--)v=(v<<8)|*p++;return v; }
int main(int argc, char **argv) {
    unsetenv("HIDAGENT_UXPLAY_FRAMED_FD");assert(hafe_configure_from_env()==0);
    for (const char *invalid : {"", "2", "-1", "99junk", "99999999999999999999999999"}) {
        setenv("HIDAGENT_UXPLAY_FRAMED_FD",invalid,1);assert(hafe_configure_from_env()==-1);
    }
    unsetenv("HIDAGENT_UXPLAY_FRAMED_FD");
    gst_init(nullptr,nullptr);
    GError *error=nullptr;
    GstElement *encoder=gst_parse_launch("videotestsrc num-buffers=1 pattern=ball ! "
        "video/x-raw,width=128,height=96,framerate=8/1 ! videoconvert ! "
        "vtenc_h264 realtime=true ! h264parse config-interval=-1 ! "
        "video/x-h264,stream-format=byte-stream,alignment=au ! appsink name=encoded sync=false",&error);
    if(error) { std::cerr<<error->message<<'\n'; return 2; }
    assert(encoder);
    GstElement *sink=gst_bin_get_by_name(GST_BIN(encoder),"encoded");
    assert(gst_element_set_state(encoder,GST_STATE_PLAYING)!=GST_STATE_CHANGE_FAILURE);
    GstSample *encoded=gst_app_sink_try_pull_sample(GST_APP_SINK(sink),5*GST_SECOND);
    assert(encoded);
    GstMapInfo map;assert(gst_buffer_map(gst_sample_get_buffer(encoded),&map,GST_MAP_READ));
    std::vector<unsigned char> h264(map.data,map.data+map.size);
    gst_buffer_unmap(gst_sample_get_buffer(encoded),&map);gst_sample_unref(encoded);
    gst_element_set_state(encoder,GST_STATE_NULL);gst_object_unref(sink);gst_object_unref(encoder);

    int fd[2];assert(pipe(fd)==0);
    std::mutex capture_mutex;std::vector<unsigned char> bytes;
    std::thread reader([&]{unsigned char b[4096];ssize_t count;while((count=read(fd[0],b,sizeof(b)))>0){
        std::lock_guard<std::mutex> lock(capture_mutex);bytes.insert(bytes.end(),b,b+count); } close(fd[0]);});
    setenv("HIDAGENT_UXPLAY_FRAMED_FD",std::to_string(fd[0]).c_str(),1);
    assert(hafe_configure_from_env()==-1); // read end must never be accepted.
    setenv("HIDAGENT_UXPLAY_FRAMED_FD",std::to_string(fd[1]).c_str(),1);
    assert(hafe_configure_from_env()==1);unsetenv("HIDAGENT_UXPLAY_FRAMED_FD");
    video_decode_struct packet={};packet.data=h264.data();packet.data_len=h264.size();packet.nal_count=3;
    auto frames=[&](uint64_t epoch) {std::lock_guard<std::mutex> lock(capture_mutex);size_t pos=0;int count=0;
        while(bytes.size()-pos>=32){auto h=bytes.data()+pos;auto len=read_be(h+24,4);if(bytes.size()-pos<32+len)break;
        if(h[5]==2 && read_be(h+8,8)==epoch)++count;pos+=32+len;}return count;};
    auto wait=[&](uint64_t epoch) {auto end=std::chrono::steady_clock::now()+std::chrono::seconds(5);
        while(!frames(epoch)&&std::chrono::steady_clock::now()<end)std::this_thread::sleep_for(std::chrono::milliseconds(10));assert(frames(epoch)>0);};
    HafeSession *old=static_cast<HafeSession*>(hafe_begin(nullptr));assert(old&&old->epoch==1);
    hafe_process(old,&packet);wait(1);int old_count=frames(1);
    HafeSession *current=static_cast<HafeSession*>(hafe_begin(nullptr));assert(current&&current->epoch==2);
    // The old decoder is still alive, but never receives current's epoch.
    hafe_process(old,&packet);hafe_process(current,&packet);wait(2);
    hafe_end(nullptr,old);assert(frames(1)==old_count);
    hafe_end(nullptr,current);writer.reset();reader.join();
    size_t pos=0;uint64_t active=0;int count=0;
    while(pos<bytes.size()){assert(bytes.size()-pos>=32);auto h=bytes.data()+pos;auto epoch=read_be(h+8,8),len=read_be(h+24,4);pos+=32;
        assert(bytes.size()-pos>=len);if(h[5]==1){assert(!active);active=epoch;}else if(h[5]==3){assert(active==epoch);active=0;}
        else{assert(h[5]==2 && active==epoch && len>100 && bytes[pos]==255 && bytes[pos+1]==216 && bytes[pos+len-2]==255 && bytes[pos+len-1]==217);++count;}pos+=len;}
    assert(!active&&count>=2);
    if(argc>1){std::ofstream out(argv[1],std::ios::binary);out.write((char*)bytes.data(),bytes.size());}
    std::cout<<"PASS: real H264 decode/appsink JPEG, two immutable session pipelines, late old pipeline cannot publish after BEGIN2 ("<<count<<" frames)\n";
}
