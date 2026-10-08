// Direct-call probe of unmodified pinned XNNPACK FP32 SIMD kernels.
// Synthetic deterministic inputs only. No model or audio data is exported.
#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <emscripten/emscripten.h>
#include "xnnpack/gemm.h"
#include "xnnpack/igemm.h"
static float *a, *w, *c[2], *zero;
static const float **indirect;
static size_t mr, nc, kc, ks, padded_nc;
static int is_igemm;
static union xnn_f32_minmax_params params;
static uint32_t rng=1;
static float rand_float(void) {rng=rng*1664525u+1013904223u;return ((int32_t)(rng>>8)-8388608)/8388608.0f;}
static void invoke(int mode) {
 if (is_igemm) {
  if (mode) xnn_f32_igemm_minmax_ukernel_4x8__wasmsimd_x86_loadsplat(mr,nc,kc*4,ks*4*sizeof(void*),indirect,w,c[mode],nc*4,8*4,0,zero,&params);
  else xnn_f32_igemm_minmax_ukernel_4x8__wasmsimd_x86_splat(mr,nc,kc*4,ks*4*sizeof(void*),indirect,w,c[mode],nc*4,8*4,0,zero,&params);
 } else {
  if (mode) xnn_f32_gemm_minmax_ukernel_4x8__wasmsimd_x86_loadsplat(mr,nc,kc*4,a,kc*4,w,c[mode],nc*4,8*4,&params);
  else xnn_f32_gemm_minmax_ukernel_4x8__wasmsimd_x86_splat(mr,nc,kc*4,a,kc*4,w,c[mode],nc*4,8*4,&params);
 }
}
EMSCRIPTEN_KEEPALIVE int setup(int ig, int m, int n, int k, int s) {
 free(a);free(w);free(c[0]);free(c[1]);free(zero);free(indirect);
 is_igemm=ig;mr=m;nc=n;kc=k;ks=ig?s:1;padded_nc=(nc+7)/8*8;rng=1;
 a=calloc(4*ks*kc+16,sizeof(float));w=calloc(padded_nc*(1+kc*ks)+16,sizeof(float));
 c[0]=calloc(4*nc+16,sizeof(float));c[1]=calloc(4*nc+16,sizeof(float));
 zero=calloc(kc+16,sizeof(float));indirect=calloc(4*ks,sizeof(float*));
 if(!a||!w||!c[0]||!c[1]||!zero||!indirect)abort();
 for(size_t i=0;i<4*ks*kc;i++)a[i]=rand_float();
 for(size_t i=0;i<padded_nc*(1+kc*ks);i++)w[i]=rand_float();
 // MR tails alias the last valid row, as XNNPACK indirection initialization does.
 for(size_t j=0;j<ks;j++)for(size_t r=0;r<4;r++)indirect[4*j+r]=a+(j*4+(r<mr?r:mr-1))*kc;
 params.scalar.min=-INFINITY;params.scalar.max=INFINITY;return 0;
}
EMSCRIPTEN_KEEPALIVE void configure_validation(int bounded, int zero_padding) {
 params.scalar.min=bounded?-0.25f:-INFINITY;
 params.scalar.max=bounded?0.5f:INFINITY;
 if(zero_padding&&is_igemm)for(size_t s=0;s<ks;s++)for(size_t r=0;r<4;r++){
  size_t real_r=r<mr?r:mr-1;
  if((s+real_r)%3==0)indirect[4*s+r]=zero;
 }
}
EMSCRIPTEN_KEEPALIVE double validate(void) {
 invoke(0);invoke(1);
 if(memcmp(c[0],c[1],mr*nc*sizeof(float)))return -1;
 double max_error=0;
 for(size_t r=0;r<mr;r++)for(size_t n=0;n<nc;n++){
  size_t tile=n/8,lane=n%8;const float* wp=w+tile*8*(1+ks*kc);
  float reference=wp[lane];
  for(size_t s=0;s<ks;s++)for(size_t k=0;k<kc;k++){
   const float* ap=is_igemm?indirect[4*s+r]:a+r*kc;
   // Match prescribed sequential FP32 multiply then add (contraction disabled).
   float prod=ap[k]*wp[8+(s*kc+k)*8+lane];reference=reference+prod;
  }
  reference=fminf(fmaxf(reference,params.scalar.min),params.scalar.max);
  if(!isfinite(reference)||!isfinite(c[0][r*nc+n])||!isfinite(c[1][r*nc+n]))return -2;
  double err=fabs((double)reference-c[0][r*nc+n]);if(!isfinite(err))return -3;if(err>max_error)max_error=err;
 }
 return max_error;
}
EMSCRIPTEN_KEEPALIVE double bench(int mode, int reps) {
 for(int i=0;i<reps;i++)invoke(mode);
 return c[mode][0];
}
