#include "hafe_stream.hpp"
#include <cassert>
#include <chrono>
#include <fstream>
#include <iostream>
#include <thread>
#include <vector>
#include <unistd.h>

static uint64_t number(const unsigned char *p, int n) { uint64_t v=0; while(n--) v=(v<<8)|*p++; return v; }
static const unsigned char old_jpeg[] = {255,216,1,255,217};
static const unsigned char new_jpeg[] = {255,216,2,255,217};
struct Capture {
    int fd[2]; std::vector<unsigned char> bytes; std::thread reader;
    Capture() { assert(pipe(fd)==0); reader=std::thread([this] {
        unsigned char data[4096]; ssize_t count;
        while((count=read(fd[0],data,sizeof(data)))>0) bytes.insert(bytes.end(),data,data+count);
        close(fd[0]);
    }); }
    void finish() { reader.join(); }
};
int main(int argc, char **argv) {
    Capture capture;
    {
        HafeWriter stream(capture.fd[1]);
        auto old = stream.begin(); assert(old == 1);
        assert(stream.jpeg(old,old_jpeg,sizeof(old_jpeg),777));
        auto current = stream.begin(); assert(current == 2);
        // Simulated delayed callbacks retain the original immutable session.
        std::thread late([&]{ for(int i=0;i<10000;i++) assert(!stream.jpeg(old,old_jpeg,sizeof(old_jpeg),777)); stream.end(old); });
        for(int i=0;i<100;i++) assert(stream.jpeg(current,new_jpeg,sizeof(new_jpeg),12345));
        late.join(); stream.end(current);
        assert(!stream.jpeg(current,new_jpeg,sizeof(new_jpeg),12345));
        stream.end(current); // duplicate native close must not duplicate END.
        auto third=stream.begin(); assert(third==3); stream.end(third);
    }
    capture.finish();
    size_t pos=0; uint64_t active=0,last_epoch=0,seq=0; int frames=0,records=0;
    while(pos<capture.bytes.size()) {
        assert(capture.bytes.size()-pos>=32);
        auto h=capture.bytes.data()+pos; assert(std::string((char*)h,4)=="HAFE");
        assert(h[4]==2 && h[6]==0 && h[7]==0);
        auto epoch=number(h+8,8), frame=number(h+16,8), len=number(h+24,4); pos+=32;
        assert(capture.bytes.size()-pos>=len);
        if(h[5]==1) { assert(!active && epoch>last_epoch && !frame && !len && number(h+28,4)==0); active=epoch; last_epoch=epoch;seq=0; }
        else if(h[5]==3) { assert(epoch==active && !frame && !len && number(h+28,4)==0); active=0; }
        else { assert(h[5]==2 && epoch==active && frame==++seq && len==5);
            assert(number(h+28,4)==(epoch==1 ? 777 : 12345));
            assert(capture.bytes[pos+2]==epoch); ++frames; }
        pos+=len; ++records;
    }
    assert(!active && frames==101 && records==107);
    if(argc>1) { std::ofstream out(argv[1],std::ios::binary); out.write((char*)capture.bytes.data(),capture.bytes.size()); }
    {
        int fd[2]; assert(pipe(fd)==0); close(fd[0]); HafeWriter stream(fd[1]);
        assert(stream.begin()==0 && !stream.healthy());
    }
    {
        Capture bad; { HafeWriter stream(bad.fd[1]);auto epoch=stream.begin();
          assert(!stream.jpeg(epoch,new_jpeg,HafeWriter::MAX_JPEG+1,12345));assert(!stream.healthy()); }
        bad.finish();
    }
    {
        int fd[2]; assert(pipe(fd)==0); HafeWriter stream(fd[1]); auto epoch=stream.begin();
        std::vector<unsigned char> huge(HafeWriter::MAX_JPEG,1);huge[0]=255;huge[1]=216;huge[huge.size()-2]=255;huge.back()=217;
        auto start=std::chrono::steady_clock::now(); assert(!stream.jpeg(epoch,huge.data(),huge.size(),12345));
        auto ms=std::chrono::duration_cast<std::chrono::milliseconds>(std::chrono::steady_clock::now()-start).count();
        assert(ms>=100 && ms<1000); assert(!stream.healthy() && stream.begin()==0);close(fd[0]);
    }
    std::cout << "PASS: 107 ordered records; 10000 stale callbacks dropped; bounded oversize/broken-pipe/backpressure failure\n";
}
