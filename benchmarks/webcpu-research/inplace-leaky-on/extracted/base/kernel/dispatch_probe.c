// Link this diagnostic helper against the actual ORT bundled archive.
// Never infer browser dispatch from Node's absent self.navigator.
#include <math.h>
#include <stdio.h>
#include <emscripten/emscripten.h>
#include "xnnpack/config.h"
#include "xnnpack/gemm.h"
#include "xnnpack/igemm.h"
EM_JS(int, probe_navigator_concurrency, (void), {
 return typeof self !== 'undefined' && self.navigator ? self.navigator.hardwareConcurrency : -1;
});
EMSCRIPTEN_KEEPALIVE int probe_xnn_f32_dispatch(void) {
 const struct xnn_hardware_config* hw=xnn_init_hardware_config();
 const struct xnn_gemm_config* config=xnn_init_f32_gemm_config();
 if(!hw||!config)return -1;
 int loadsplat=0,splat=0,total=0;
#define TEST(kind,activation,prefix,m,arch) do { \
 xnn_##kind##_ukernel_fn f=config->activation.kind[((m)-1)].function[0]; \
 loadsplat+=(f==(xnn_##kind##_ukernel_fn)xnn_f32_##kind##_##prefix##ukernel_##m##x8__wasmsimd##arch##_loadsplat); \
 splat+=(f==(xnn_##kind##_ukernel_fn)xnn_f32_##kind##_##prefix##ukernel_##m##x8__wasmsimd##arch##_splat); \
 total++; \
} while(0)
#define BOTH(kind,m) TEST(kind,linear,,m,); TEST(kind,minmax,minmax_,m,_x86); TEST(kind,relu,relu_,m,)
 BOTH(gemm,1);BOTH(gemm,4);BOTH(igemm,1);BOTH(igemm,4);
#undef BOTH
#undef TEST
 printf("XNN_DISPATCH {\"is_x86\":%d,\"navigatorHardwareConcurrency\":%d,\"mr\":%u,\"nr\":%u,\"loadsplatPointers\":%d,\"splatPointers\":%d,\"checkedPointers\":%d,\"relaxedSimd\":%d}\n",hw->is_x86,probe_navigator_concurrency(),config->mr,config->nr,loadsplat,splat,total,XNN_ARCH_WASMRELAXEDSIMD);
 return loadsplat==total ? 1 : splat==total ? 0 : -2;
}
#ifdef DISPATCH_PROBE_MAIN
int main(void){return probe_xnn_f32_dispatch()<0?1:0;}
#endif
