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
    auto begins=[&](std::vector<uint64_t> &out) {std::lock_guard<std::mutex> lock(capture_mutex);size_t pos=0;
        while(bytes.size()-pos>=32){auto h=bytes.data()+pos;auto len=read_be(h+24,4);if(bytes.size()-pos<32+len)break;
        if(h[5]==1)out.push_back(read_be(h+8,8));pos+=32+len;}};
    auto stamps=[&](uint64_t epoch, std::vector<uint32_t> &out) {std::lock_guard<std::mutex> lock(capture_mutex);size_t pos=0;
        while(bytes.size()-pos>=32){auto h=bytes.data()+pos;auto len=read_be(h+24,4);if(bytes.size()-pos<32+len)break;
        if(h[5]==2 && read_be(h+8,8)==epoch)out.push_back((uint32_t)read_be(h+28,4));pos+=32+len;}};
    HafeSession *old=static_cast<HafeSession*>(hafe_begin(nullptr));assert(old&&old->epoch==1);
    hafe_process(old,&packet);wait(1);int old_count=frames(1);
    HafeSession *current=static_cast<HafeSession*>(hafe_begin(nullptr));assert(current&&current->epoch==2);
    // The old decoder is still alive, but never receives current's epoch.
    hafe_process(old,&packet);hafe_process(current,&packet);wait(2);
    // Burst regression: 30 distinct frames fed back-to-back must all survive
    // the pre-decode queue. With leaky=downstream + 2 buffers the oldest
    // compressed AUs (possibly the IDR carrying SPS/PPS) were silently
    // dropped while the decoder drained, stalling decode until the next IDR
    // and freezing the web mirror on a stale frame. With leaky=no + 16
    // buffers the queue applies bounded backpressure instead of dropping.
    // allow-frame-reordering=false: without an EOS the decoder would hold
    // back B-frames, which would look like drops; the queue is what is
    // under test here, not decoder latency. 720p makes decode+convert+JPEG
    // slow enough that a back-to-back burst actually fills the pre-decode
    // queue: at 128x96 the decoder kept up and even the old 2-buffer queue
    // passed, so the burst proved nothing there.
    error=nullptr;
    GstElement *burst_encoder=gst_parse_launch("videotestsrc num-buffers=30 pattern=ball ! "
        "video/x-raw,width=1280,height=720,framerate=8/1 ! videoconvert ! "
        "vtenc_h264 realtime=true allow-frame-reordering=false ! h264parse config-interval=-1 ! "
        "video/x-h264,stream-format=byte-stream,alignment=au ! appsink name=encoded sync=false",&error);
    if(error) { std::cerr<<error->message<<'\n'; return 2; }
    assert(burst_encoder);
    GstElement *burst_sink=gst_bin_get_by_name(GST_BIN(burst_encoder),"encoded");
    assert(gst_element_set_state(burst_encoder,GST_STATE_PLAYING)!=GST_STATE_CHANGE_FAILURE);
    std::vector<std::vector<unsigned char>> burst;
    for (int i=0;i<30;++i) {
        GstSample *sample=gst_app_sink_try_pull_sample(GST_APP_SINK(burst_sink),5*GST_SECOND);
        assert(sample);
        GstMapInfo info;assert(gst_buffer_map(gst_sample_get_buffer(sample),&info,GST_MAP_READ));
        burst.emplace_back(info.data,info.data+info.size);
        gst_buffer_unmap(gst_sample_get_buffer(sample),&info);gst_sample_unref(sample);
    }
    gst_element_set_state(burst_encoder,GST_STATE_NULL);gst_object_unref(burst_sink);gst_object_unref(burst_encoder);
    HafeSession *burst_session=static_cast<HafeSession*>(hafe_begin(nullptr));assert(burst_session);
    const uint64_t burst_epoch=burst_session->epoch;
    for (auto &frame : burst) {
        video_decode_struct burst_packet={};burst_packet.data=frame.data();
        burst_packet.data_len=frame.size();burst_packet.nal_count=3;
        hafe_process(burst_session,&burst_packet);
    }
    { auto deadline=std::chrono::steady_clock::now()+std::chrono::seconds(5);
      while(frames(burst_epoch)<29&&std::chrono::steady_clock::now()<deadline)
          std::this_thread::sleep_for(std::chrono::milliseconds(10)); }
    assert(frames(burst_epoch)>=29);
    // Overload recovery: feeding bursts back-to-back fills the 8MB appsrc
    // queue faster than the software decoder drains it. The old code ended
    // the session on overflow and froze the mirror until a new AirPlay
    // connection. Now the pipeline is torn down and a fresh epoch begins;
    // the next burst (starting with an IDR) re-syncs the new decoder.
    uint64_t recovered_epoch = 0;
    size_t fed_bytes = 0, burst_bytes = 0;
    for (auto &frame : burst) burst_bytes += frame.size();
    std::cout<<"overflow test: burst is "<<burst_bytes<<" bytes ("<<burst_bytes/30<<" avg AU), feeding to 48MB\n";
    // Feed by total bytes, not rounds: a 720p ball AU is only a few KB, so a
    // fixed round count can stay under the 8MB appsrc queue. The feed rate
    // (back-to-back pushes) far exceeds the software decoder drain rate, so
    // the queue must fill once enough bytes are fed.
    for (; !recovered_epoch && fed_bytes < 48u*1024u*1024u;) {
        for (auto &frame : burst) {
            video_decode_struct p={}; p.data=frame.data(); p.data_len=frame.size(); p.nal_count=3;
            hafe_process(burst_session,&p); fed_bytes += frame.size();
        }
        std::vector<uint64_t> seen; begins(seen);
        for (auto e : seen) if (e > burst_epoch) recovered_epoch = e;
    }
    std::cout<<"overflow test fed "<<fed_bytes<<" bytes, recovered epoch "<<recovered_epoch<<"\n";
    assert(recovered_epoch > burst_epoch);
    // Feed one more burst so the new decoder gets its IDR, then wait for JPEGs
    // on the newest epoch (a second overload during this burst would restart
    // again; take whatever epoch is latest).
    for (auto &frame : burst) {
        video_decode_struct p={}; p.data=frame.data(); p.data_len=frame.size(); p.nal_count=3;
        hafe_process(burst_session,&p);
    }
    { std::vector<uint64_t> seen; begins(seen);
      for (auto e : seen) if (e > recovered_epoch) recovered_epoch = e; }
    { auto deadline=std::chrono::steady_clock::now()+std::chrono::seconds(5);
      while(frames(recovered_epoch)<1&&std::chrono::steady_clock::now()<deadline)
          std::this_thread::sleep_for(std::chrono::milliseconds(10)); }
    assert(frames(recovered_epoch)>=1);
    // HAFE v2: the new epoch's JPEGs carry a version-2 header and a nonzero,
    // non-decreasing capture timestamp (monotonic ms at push).
    { std::vector<uint32_t> ts; stamps(recovered_epoch, ts);
      assert(!ts.empty());
      for (size_t i=0;i<ts.size();++i) {
          assert(ts[i]!=0);
          if (i) assert(ts[i]>=ts[i-1]);
      }
      std::lock_guard<std::mutex> lock(capture_mutex); size_t pos=0;
      while(bytes.size()-pos>=32){auto h=bytes.data()+pos;auto len=read_be(h+24,4);if(bytes.size()-pos<32+len)break;
      if(read_be(h+8,8)==recovered_epoch) assert(h[4]==2); pos+=32+len;} }
    hafe_end(nullptr,burst_session);
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
