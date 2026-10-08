#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <pthread.h>
#include <emscripten.h>
#include "xnnpack.h"
#include "xnnpack/operator.h"
#include "xnnpack/compute.h"
#include "xnnpack/igemm.h"
#include "pthreadpool.h"
static xnn_operator_t op;
static pthreadpool_t pool;
static float *in[2], *out, *weights, *bias;
static uint8_t *obuf,*packed_snapshot,*indirect_snapshot;
static size_t packed_size,indirect_size;
static size_t M,C,K,D,osz,isz,ntasks;
static xnn_igemm_ukernel_fn real_kernel;
static pthreadpool_task_2d_tile_2d_t real_task;
struct task_record { uintptr_t thread; size_t m,n,mc,nc,calls,visits; size_t ms[32],ns[32],mrs[32],nrs[32]; int bad; };
static struct task_record *records;
static _Thread_local struct task_record *current;
static uint32_t rng;
static float value(void) {rng^=rng<<13;rng^=rng>>17;rng^=rng<<5; return ((int)(rng%2049)-1024)*0.0009765625f;}
static void kernel(size_t mr,size_t nr,size_t kc,size_t ks,const void** a,const void* w,void* c,size_t cm,size_t cn,size_t offset,const void* zero,const void* params) {
 struct igemm_context *ctx=&op->context.igemm.igemm;
 size_t delta=(uintptr_t)c-(uintptr_t)ctx->c,m=delta/cm,n=(delta%cm)/4;
 struct task_record *r=current;size_t i=r->calls++;
 if(i<32){r->ms[i]=m;r->ns[i]=n;r->mrs[i]=mr;r->nrs[i]=nr;}else r->bad=1;
 if(m<r->m||m+mr>r->m+r->mc||n<r->n||n+nr>r->n+r->nc||
 (uintptr_t)a!=(uintptr_t)ctx->indirect_a+m*ctx->ks*sizeof(void*)||
 (uintptr_t)w!=(uintptr_t)ctx->packed_w+n*ctx->w_stride||kc!=ctx->kc||ks!=ctx->ks_scaled||cm!=ctx->cm_stride||cn!=ctx->cn_stride||params!=&ctx->params||offset!=ctx->a_offset||zero!=ctx->zero)r->bad=1;
 real_kernel(mr,nr,kc,ks,a,w,c,cm,cn,offset,zero,params);
}
static void task(void* v,size_t m,size_t n,size_t mc,size_t nc){
 size_t cols=(C+op->compute[0].tile[1]-1)/op->compute[0].tile[1];
 size_t id=m/op->compute[0].tile[0]*cols+n/op->compute[0].tile[1];
 struct task_record *r=&records[id];__atomic_fetch_add(&r->visits,1,__ATOMIC_RELAXED);current=r;r->thread=(uintptr_t)pthread_self();r->m=m;r->n=n;r->mc=mc;r->nc=nc;
 real_task(v,m,n,mc,nc);current=NULL;
}
#define CHECK(x) do { int status=(x); if(status){printf("FAIL %s %d\n",#x,status);return status;} }while(0)
EMSCRIPTEN_KEEPALIVE int setup(int width,int channels,int kw,int dilation,int seed){
 M=width;C=channels;K=kw;D=dilation;isz=M*C;osz=M*C;rng=seed;
 if(!pool){CHECK(xnn_initialize(NULL));pool=pthreadpool_create(2);if(!pool||pthreadpool_get_threads_count(pool)!=2)return 90;}
 weights=aligned_alloc(64,((C*C*K*4+63)/64)*64);bias=aligned_alloc(64,((C*4+63)/64)*64);
 for(size_t i=0;i<C*C*K;i++)weights[i]=value()*0.0625f;for(size_t i=0;i<C;i++)bias[i]=value();
 for(int j=0;j<2;j++){in[j]=aligned_alloc(64,((isz*4+XNN_EXTRA_BYTES+63)/64)*64);for(size_t i=0;i<isz;i++)in[j][i]=value();memset(in[j]+isz,0,XNN_EXTRA_BYTES);}
 obuf=aligned_alloc(64,((osz*4+128+63)/64)*64);out=(float*)(obuf+64);
 CHECK(xnn_create_convolution2d_nhwc_f32(0,(K/2)*D,0,(K/2)*D,1,K,1,1,1,D,1,C,C,C,C,weights,bias,-INFINITY,INFINITY,0,NULL,NULL,&op));
 size_t ws,wa,oh,ow;CHECK(xnn_reshape_convolution2d_nhwc_f32(op,1,1,M,&ws,&wa,&oh,&ow,pool));
 if(ws||oh!=1||ow!=M)return 91;
 struct igemm_context *ctx=&op->context.igemm.igemm;
 real_kernel=ctx->ukernel.function[0];real_task=op->compute[0].task_2d_tile_2d;
 int which=real_task==(pthreadpool_task_2d_tile_2d_t)xnn_compute_igemm?0:real_task==(pthreadpool_task_2d_tile_2d_t)xnn_compute_igemm_m8?1:real_task==(pthreadpool_task_2d_tile_2d_t)xnn_compute_igemm_m8_nc32?2:-1;
 printf("SELECT {\"m\":%zu,\"c\":%zu,\"k\":%zu,\"d\":%zu,\"seed\":%d,\"which\":%d,\"mr\":%zu,\"nr\":%zu,\"tile\":[%zu,%zu],\"range\":[%zu,%zu],\"ks\":%zu,\"ks_scaled\":%zu,\"kc\":%zu,\"w_stride\":%zu,\"cm_stride\":%zu,\"cn_stride\":%zu,\"alignment\":%zu,\"kernel_mr4_linear_loadsplat\":%d,\"threads\":%zu}\n",M,C,K,D,seed,which,ctx->ks_scaled/(ctx->ks*4),ctx->cn_stride/4,op->compute[0].tile[0],op->compute[0].tile[1],op->compute[0].range[0],op->compute[0].range[1],ctx->ks,ctx->ks_scaled,ctx->kc,ctx->w_stride,ctx->cm_stride,ctx->cn_stride,(uintptr_t)out%64,real_kernel==(xnn_igemm_ukernel_fn)xnn_f32_igemm_ukernel_4x8__wasmsimd_loadsplat,pthreadpool_get_threads_count(pool));
 ntasks=((M+op->compute[0].tile[0]-1)/op->compute[0].tile[0])*((C+op->compute[0].tile[1]-1)/op->compute[0].tile[1]);
 packed_size=ctx->w_stride*C;indirect_size=((M+(ctx->ks_scaled/(ctx->ks*4))-1)/(ctx->ks_scaled/(ctx->ks*4)))*(ctx->ks_scaled/(ctx->ks*4))*K*4;
 packed_snapshot=malloc(packed_size);indirect_snapshot=malloc(indirect_size);memcpy(packed_snapshot,ctx->packed_w,packed_size);memcpy(indirect_snapshot,ctx->indirect_a,indirect_size);
 records=calloc(ntasks,sizeof(*records));ctx->ukernel.function[0]=kernel;op->compute[0].task_2d_tile_2d=task;
 return 0;
}
EMSCRIPTEN_KEEPALIVE int run_case(int input){
 memset(records,0,ntasks*sizeof(*records));memset(obuf,0xA5,osz*4+128);
 CHECK(xnn_setup_convolution2d_nhwc_f32(op,NULL,in[input],out));CHECK(xnn_run_operator(op,pool));
 uintptr_t ids[4]={0};size_t counts[4]={0},calls=0,callbacks=0;int bad=0;size_t coverage=0;
 for(size_t i=0;i<ntasks;i++){struct task_record*r=&records[i];int j=0;while(j<4&&ids[j]&&ids[j]!=r->thread)j++;if(j==4||!r->thread){bad++;continue;}ids[j]=r->thread;counts[j]++;calls+=r->calls;callbacks+=r->visits;bad+=r->bad;if(r->visits!=1)bad++;
 size_t tilem=op->compute[0].tile[0],tilen=op->compute[0].tile[1],cols=(C+tilen-1)/tilen;
 size_t em=i/cols*tilem,en=i%cols*tilen;
 if(r->m!=em||r->n!=en||r->mc!=(M-em<tilem?M-em:tilem)||r->nc!=(C-en<tilen?C-en:tilen))bad++;
 size_t area=0;
 for(size_t a=0;a<r->calls&&a<32;a++){area+=r->mrs[a]*r->nrs[a];for(size_t b=0;b<a;b++)if(r->ms[a]<r->ms[b]+r->mrs[b]&&r->ms[b]<r->ms[a]+r->mrs[a]&&r->ns[a]<r->ns[b]+r->nrs[b]&&r->ns[b]<r->ns[a]+r->nrs[a])bad++;}if(area!=r->mc*r->nc)bad++;coverage+=area;
 }
 for(int i=0;i<64;i++)if(obuf[i]!=0xA5||obuf[64+osz*4+i]!=0xA5)bad++;
 if(coverage!=osz)bad++;
 struct igemm_context *ctx=&op->context.igemm.igemm;
 if(memcmp(packed_snapshot,ctx->packed_w,packed_size)||memcmp(indirect_snapshot,ctx->indirect_a,indirect_size))bad++;
 size_t mr=ctx->ks_scaled/(ctx->ks*sizeof(void*));
 for(size_t tile=0;tile<M;tile+=mr)for(size_t k=0;k<K;k++)for(size_t lane=0;lane<mr;lane++){
  size_t ox=tile+lane<M?tile+lane:M-1;int ix=(int)ox+(int)(k*D)-(int)((K/2)*D);
  const void *ptr=ctx->indirect_a[tile*K+k*mr+lane];
  if(ix<0||ix>=(int)M){if(ptr!=ctx->zero)bad++;}
  else if(ptr==ctx->zero||(uintptr_t)ptr+ctx->a_offset!=(uintptr_t)(in[input]+ix*C))bad++;
 }
 printf("RUN {\"input\":%d,\"callbacks\":%zu,\"calls\":%zu,\"bad\":%d,\"thread_ids\":[%zu,%zu],\"thread_callbacks\":[%zu,%zu],\"first_trace\":[",input,callbacks,calls,bad,(size_t)ids[0],(size_t)ids[1],counts[0],counts[1]);
 for(size_t i=0;i<records[0].calls&&i<32;i++)printf("%s[%zu,%zu,%zu,%zu]",i?",":"",records[0].ms[i],records[0].ns[i],records[0].mrs[i],records[0].nrs[i]);puts("]}");return bad?92:0;
}
EMSCRIPTEN_KEEPALIVE uintptr_t output(void){return (uintptr_t)out;}
EMSCRIPTEN_KEEPALIVE size_t output_bytes(void){return osz*4;}
EMSCRIPTEN_KEEPALIVE void cleanup(void){xnn_delete_operator(op);free(records);free(weights);free(bias);free(in[0]);free(in[1]);free(obuf);free(packed_snapshot);free(indirect_snapshot);}

EMSCRIPTEN_KEEPALIVE uintptr_t packed(void){return (uintptr_t)op->context.igemm.igemm.packed_w;}
EMSCRIPTEN_KEEPALIVE size_t packed_bytes(void){return packed_size;}
