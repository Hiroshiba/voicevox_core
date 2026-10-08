#include <math.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <emscripten.h>
#include "xnnpack.h"
#include "xnnpack/operator.h"
#include "xnnpack/compute.h"
#include "xnnpack/igemm.h"
#include "pthreadpool.h"
#define M 61568
#define C 128
#define K 11
struct fixture { xnn_operator_t op; float *input[2],*output,*weights,*bias; };
static struct fixture fixtures[3];
static pthreadpool_t pool;
static uint32_t rng;
static float value(void){rng^=rng<<13;rng^=rng>>17;rng^=rng<<5;return ((int)(rng%2049)-1024)*0.0009765625f;}
static void* alloc(size_t bytes){return aligned_alloc(64,(bytes+63)/64*64);}
EMSCRIPTEN_KEEPALIVE int initialize(void){
 int status=xnn_initialize(NULL);if(status)return status;
 pool=pthreadpool_create(2);if(!pool||pthreadpool_get_threads_count(pool)!=2)return 90;
 for(int q=0;q<3;q++){
  int d=2*q+1;struct fixture*f=&fixtures[q];rng=17;
  f->weights=alloc(C*C*K*4);f->bias=alloc(C*4);f->output=alloc(M*C*4);
  if(!f->weights||!f->bias||!f->output)return 91;
  for(size_t i=0;i<C*C*K;i++)f->weights[i]=value()*0.0625f;
  for(size_t i=0;i<C;i++)f->bias[i]=value();
  for(int a=0;a<2;a++){f->input[a]=alloc(M*C*4+XNN_EXTRA_BYTES);if(!f->input[a])return 91;for(size_t i=0;i<M*C;i++)f->input[a][i]=value();memset(f->input[a]+M*C,0,XNN_EXTRA_BYTES);}
  status=xnn_create_convolution2d_nhwc_f32(0,5*d,0,5*d,1,K,1,1,1,d,1,C,C,C,C,f->weights,f->bias,-INFINITY,INFINITY,0,NULL,NULL,&f->op);if(status)return status;
 }
 return 0;
}
/* Primary boundary: no counters, hashes, tracing, clocks, or heap allocation. */
EMSCRIPTEN_KEEPALIVE int cycle(int q,int input){
 struct fixture*f=&fixtures[q];size_t workspace_size=0,workspace_alignment=0;
 int status=xnn_reshape_convolution2d_nhwc_f32(f->op,1,1,M,&workspace_size,&workspace_alignment,NULL,NULL,pool);
 if(status)return status;
 if(workspace_size!=0)return 92;
 status=xnn_setup_convolution2d_nhwc_f32(f->op,NULL,f->input[input],f->output);
 if(status)return status;
 return xnn_run_operator(f->op,pool);
}
/* Read-only descriptor checks are invoked only outside measured intervals. */
EMSCRIPTEN_KEEPALIVE int selection(int q,int condition){
 struct fixture*f=&fixtures[q];struct igemm_context*ctx=&f->op->context.igemm.igemm;
 pthreadpool_task_2d_tile_2d_t task=f->op->compute[0].task_2d_tile_2d;
 pthreadpool_task_2d_tile_2d_t expected=(pthreadpool_task_2d_tile_2d_t)(condition==0?xnn_compute_igemm:condition==1?xnn_compute_igemm_m8:xnn_compute_igemm_m8_nc32);
 return task==expected && ctx->ukernel.function[0]==(xnn_igemm_ukernel_fn)xnn_f32_igemm_ukernel_4x8__wasmsimd_loadsplat && f->op->compute[0].tile[0]==(condition==0?4:32) && f->op->compute[0].tile[1]==128 && f->op->compute[0].range[0]==M && f->op->compute[0].range[1]==C && ctx->ks==K && ctx->ks_scaled==176 && ctx->kc==512 && ctx->cm_stride==512 && ctx->cn_stride==32 && ((uintptr_t)f->output%64)==0 && pthreadpool_get_threads_count(pool)==2;
}
EMSCRIPTEN_KEEPALIVE uintptr_t output(int q){return (uintptr_t)fixtures[q].output;}
EMSCRIPTEN_KEEPALIVE size_t output_bytes(void){return M*C*4;}
EMSCRIPTEN_KEEPALIVE uintptr_t packed(int q){return (uintptr_t)fixtures[q].op->context.igemm.igemm.packed_w;}
EMSCRIPTEN_KEEPALIVE size_t packed_bytes(int q){return fixtures[q].op->context.igemm.igemm.w_stride*C;}
