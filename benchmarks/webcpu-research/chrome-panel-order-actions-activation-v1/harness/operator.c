/* Source-only whole-operator screen. No manual macro dispatch in measured path.
 * Fixed timing and eleven qualification fixture families; caller-owned red zones; internal production allocator untouched.
 */
#include <math.h>
#include <stddef.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <pthread.h>
#include <stdatomic.h>
#include "xnnpack.h"
#include "xnnpack/operator.h"
#include "xnnpack/igemm.h"
#include "xnnpack/microkernel-type.h"
#if defined(VVX_KSB_ARM)
#include "vvx_ksb_20261011.h"
#endif
#define M 61568u
#define N 128u
#define WORDS (M*N)
#define IO_BYTES (WORDS*4u)
#define RAW_WEIGHT_BYTES (128u*11u*128u*4u)
#define PACKED_BYTES 721408u
#define INDIRECT_BYTES (M*11u*4u)
#define ZERO_BYTES (XNN_EXTRA_BYTES + 128u * sizeof(float))
_Static_assert(ZERO_BYTES==528u, "pinned Wasm F32 zero allocation extent");
#define RED 256u
#define MAX_ALLOCS 128u
struct allocation { void* raw; unsigned char* data; size_t bytes; };
static struct allocation allocations[MAX_ALLOCS];
static unsigned allocation_error;
static xnn_operator_t op;
static pthreadpool_t pool;
static float *input,*weights,*bias,*output;
static unsigned dilation,diagnostic;
/* Dedicated quiescent alias challenge: no allocation or operator work.
 * Compare every prior heap at this SAME offset before/after a changed write,
 * restore the word, then reject any cross-heap change. Raw offset stays in page. */
static uint32_t alias_probe;
uintptr_t screen_alias_probe_ptr(void) { return (uintptr_t)&alias_probe; }
#include "fixtures.inc"
/* Private preparation diagnostics, read only outside screen_run. UINT32_MAX
 * denotes unavailable fields; no pointer values or payload bytes are exposed. */
struct prepare_diagnostics {
 uint32_t stage,return_code,last_api_status,geometry_code,created,reshaped,setup;
 uint32_t requested_dilation,requested_warmup,workspace_size,workspace_alignment,output_height,output_width;
};
static struct prepare_diagnostics prepare_diag={.return_code=UINT32_MAX,.last_api_status=UINT32_MAX,.geometry_code=UINT32_MAX,.workspace_size=UINT32_MAX,.workspace_alignment=UINT32_MAX,.output_height=UINT32_MAX,.output_width=UINT32_MAX};
struct lifecycle_diagnostics {
 uint32_t initialize_called,initialize_status,initialize_return_code,library_initialized;
 uint32_t pool_created,pool_destroy_invoked,pool_destroy_returned,deinitialize_invoked,deinitialize_status;
};
static struct lifecycle_diagnostics lifecycle_diag={.initialize_status=UINT32_MAX,.initialize_return_code=UINT32_MAX,.deinitialize_status=UINT32_MAX};
static int initialize_return(int code) { lifecycle_diag.initialize_return_code=(uint32_t)code;return code; }
static int prepare_return(int code) { prepare_diag.return_code=(uint32_t)code;return code; }
static struct compute_parameters selected;
static atomic_uint seen[M/4], callback_calls, participant_a, participant_b, coverage_errors;

static struct allocation* find_allocation(void* p) {
  for (unsigned i=0;i<MAX_ALLOCS;i++) if(allocations[i].data==p) return &allocations[i];
  return NULL;
}
static void* guarded_allocate(void* unused,size_t alignment,size_t bytes) {
  (void)unused;
  if(alignment<16) alignment=16;
  if(!alignment || (alignment&(alignment-1)) || bytes>SIZE_MAX-alignment-2*RED) {allocation_error=1;return NULL;}
  unsigned i;for(i=0;i<MAX_ALLOCS && allocations[i].raw;i++);if(i==MAX_ALLOCS){allocation_error=2;return NULL;}
  unsigned char* raw=malloc(bytes+alignment+2*RED);if(!raw)return NULL;
  unsigned char* data=(unsigned char*)(((uintptr_t)raw+RED+alignment-1)&~(uintptr_t)(alignment-1));
  memset(data-RED,0xA5,RED);memset(data+bytes,0x5A,RED);
  allocations[i]=(struct allocation){raw,data,bytes};return data;
}
static void dealloc(void* unused,void* p) {
  (void)unused;if(!p)return;struct allocation* a=find_allocation(p);
  if(!a){allocation_error=3;return;}
  for(size_t j=0;j<RED;j++)if(a->data[-(ptrdiff_t)RED+(ptrdiff_t)j]!=0xA5 || a->data[a->bytes+j]!=0x5A)allocation_error=4;
  free(a->raw);memset(a,0,sizeof(*a));
}
int screen_canaries(void) {
  unsigned bad=allocation_error;
  for(unsigned i=0;i<MAX_ALLOCS;i++)if(allocations[i].raw)
    for(size_t j=0;j<RED;j++)bad+=(allocations[i].data[-(ptrdiff_t)RED+(ptrdiff_t)j]!=0xA5)+(allocations[i].data[allocations[i].bytes+j]!=0x5A);
  return (int)bad;
}
static void observed(void* context,size_t m,size_t n,size_t ms,size_t ns) {
  unsigned tile=(unsigned)selected.tile[0];
  if(n!=0 || ns!=128 || m>=M || ms!=tile || m%tile){atomic_fetch_add(&coverage_errors,1);return;}
  if(atomic_fetch_add(&seen[m/tile],1)!=0)atomic_fetch_add(&coverage_errors,1);
  atomic_fetch_add(&callback_calls,1);
  unsigned id=(unsigned)(uintptr_t)pthread_self();
  unsigned empty=0;atomic_compare_exchange_strong(&participant_a,&empty,id);
  if(atomic_load(&participant_a)!=id){empty=0;atomic_compare_exchange_strong(&participant_b,&empty,id);if(atomic_load(&participant_b)!=id)atomic_fetch_add(&coverage_errors,1);}
  selected.task_2d_tile_2d(context,m,n,ms,ns);
}
int screen_init(void) {
  lifecycle_diag.initialize_called=1;
  if(pool || op)return initialize_return(1);
  enum xnn_status status=xnn_initialize(NULL);lifecycle_diag.initialize_status=(uint32_t)status;
  if(status!=xnn_status_success)return initialize_return(2);
  lifecycle_diag.library_initialized=1;
  pool=pthreadpool_create(2);lifecycle_diag.pool_created=(pool!=NULL);
  return initialize_return((!pool || pthreadpool_get_threads_count(pool)!=2)?3:0);
}
static pthreadpool_task_2d_tile_2d_t expected_callback(void) {
#if !defined(VVX_KSB_ARM)
 return (pthreadpool_task_2d_tile_2d_t)xnn_compute_igemm;
#elif VVX_KSB_ARM == 1
 return (pthreadpool_task_2d_tile_2d_t)vvx_ksb_20261011_B;
#elif VVX_KSB_ARM == 2
 return (pthreadpool_task_2d_tile_2d_t)vvx_ksb_20261011_P;
#else
 return (pthreadpool_task_2d_tile_2d_t)vvx_ksb_20261011_Q;
#endif
}
static unsigned expected_tile(void) {
#if defined(VVX_KSB_ARM)
 return 32;
#else
 return 4;
#endif
}
/* Read actual post-reshape fields; never fill or manually select a descriptor. */
static int verify_geometry(unsigned before_setup) {
 if(!op || !pool)return 10;
 const struct igemm_context* c=&op->context.igemm.igemm;
 const struct compute_parameters* t=&op->compute[0];
 if(op->type!=xnn_operator_type_convolution_nhwc_f32 || op->ukernel.type!=xnn_microkernel_type_igemm || op->flags || op->weights_cache || op->batch_size!=1 || op->groups!=1)return 11;
 if(op->input_height!=1 || op->output_height!=1 || op->input_width!=M || op->output_width!=M || op->group_input_channels!=128 || op->group_output_channels!=128 || op->input_pixel_stride!=128 || op->output_pixel_stride!=128)return 12;
 if(op->kernel_height!=1 || op->kernel_width!=11 || op->stride_height!=1 || op->stride_width!=1 || op->dilation_height!=1 || op->dilation_width!=dilation || op->padding_top || op->padding_bottom || op->padding_left!=5*dilation || op->padding_right!=5*dilation)return 13;
 if(op->ukernel.igemm.mr!=4 || op->ukernel.igemm.nr!=8 || op->ukernel.igemm.kr!=1 || op->ukernel.igemm.sr!=1 || c->ukernel.function[0]!=(xnn_igemm_ukernel_fn)xnn_f32_igemm_ukernel_4x8__wasmsimd_loadsplat || op->ukernel.igemm.igemm_cases[3].function[0]!=c->ukernel.function[0])return 14;
 if(t->type!=xnn_parallelization_type_2d_tile_2d || t->task_2d_tile_2d!=expected_callback() || t->context_offset || t->range[0]!=M || t->range[1]!=128 || t->tile[0]!=expected_tile() || t->tile[1]!=128)return 15;
 for(unsigned i=2;i<6;i++)if(t->range[i])return 16;
 if(op->compute[1].type!=xnn_parallelization_type_invalid || op->compute[2].type!=xnn_parallelization_type_invalid || pthreadpool_get_threads_count(pool)!=2)return 17;
 if(c->ks!=11 || c->ks_scaled!=176 || c->kc!=512 || c->w_stride!=5636 || c->cm_stride!=512 || c->cn_stride!=32 || c->ga_stride!=512 || c->gw_stride!=PACKED_BYTES || c->gc_stride!=512 || c->ba_stride!=IO_BYTES || c->bc_stride!=IO_BYTES || c->log2_csize!=2)return 18;
 if(c->zero_buffers || op->zero_size!=ZERO_BYTES || c->zero_size!=(before_setup?0:op->zero_size) || c->quantization_params || op->quantization_params || !c->indirect_a || !c->zero || !c->packed_w || c->indirect_a!=op->indirection_buffer || c->zero!=op->zero_buffer || c->packed_w!=op->packed_weights.pointer)return 19;
 if(before_setup){if(c->c || c->a_offset || op->state!=xnn_run_state_needs_setup)return 20;}
 else if(c->c!=output || op->state!=xnn_run_state_ready)return 21;
 return screen_canaries()?22:0;
}
static int screen_prepare_common(unsigned d,unsigned warmup,unsigned fixture_id) {
 prepare_diag=(struct prepare_diagnostics){.stage=1,.return_code=UINT32_MAX,.last_api_status=UINT32_MAX,.geometry_code=UINT32_MAX,.requested_dilation=d,.requested_warmup=warmup,.workspace_size=UINT32_MAX,.workspace_alignment=UINT32_MAX,.output_height=UINT32_MAX,.output_width=UINT32_MAX};
 if(op || !pool || (d!=1 && d!=3 && d!=5) || warmup>1 || fixture_id>11)return prepare_return(30);
 prepare_diag.stage=2;
 dilation=d;diagnostic=warmup;active_fixture=fixture_id;unsigned seed=d==1?17:d==3?29:43;
 input=guarded_allocate(NULL,64,IO_BYTES+64);weights=guarded_allocate(NULL,64,RAW_WEIGHT_BYTES);bias=guarded_allocate(NULL,64,128*4);output=guarded_allocate(NULL,64,IO_BYTES);
 if(!input || !weights || !bias || !output)return prepare_return(31);
 if(!fixture_id){
 for(size_t i=0;i<WORDS;i++) input[i]=((int)((17*i+13*(i/128)+19*seed)%257)-128)*0x1p-10f;
 for(size_t o=0;o<128;o++)for(size_t p=0;p<11;p++)for(size_t k=0;k<128;k++)weights[(o*11+p)*128+k]=((int)((29*o+43*p+11*k+7*seed)%127)-63)*0x1p-11f;
 for(size_t o=0;o<128;o++)bias[o]=((int)((5*o+seed)%31)-15)*0x1p-8f;
 }else fixture_fill(fixture_id,d);
 memset((unsigned char*)input+IO_BYTES,0,64);
 for(size_t i=0;i<WORDS;i++){const uint32_t poison=0x7FC12345u;memcpy(output+i,&poison,4);}
 prepare_diag.stage=3;
 enum xnn_status status=xnn_create_convolution2d_nhwc_f32(0,5*d,0,5*d,1,11,1,1,1,d,1,128,128,128,128,weights,bias,-INFINITY,INFINITY,0,NULL,NULL,&op);
 prepare_diag.last_api_status=(uint32_t)status;
 if(status!=xnn_status_success)return prepare_return(40+(int)status);
 prepare_diag.created=1;prepare_diag.stage=4;
 size_t ws=SIZE_MAX,wa=0,oh=0,ow=0;
 status=xnn_reshape_convolution2d_nhwc_f32(op,1,1,M,&ws,&wa,&oh,&ow,pool);
 prepare_diag.last_api_status=(uint32_t)status;prepare_diag.workspace_size=(uint32_t)ws;prepare_diag.workspace_alignment=(uint32_t)wa;prepare_diag.output_height=(uint32_t)oh;prepare_diag.output_width=(uint32_t)ow;
 prepare_diag.reshaped=(status==xnn_status_success);
 if(status!=xnn_status_success || ws!=0 || oh!=1 || ow!=M)return prepare_return(50+(int)status);
 prepare_diag.stage=5;
 int check=verify_geometry(1);prepare_diag.geometry_code=(uint32_t)check;if(check)return prepare_return(100+check);
 prepare_diag.stage=6;
 status=xnn_setup_convolution2d_nhwc_f32(op,NULL,input,output);
 prepare_diag.last_api_status=(uint32_t)status;
 if(status!=xnn_status_success)return prepare_return(60+(int)status);
 prepare_diag.setup=1;prepare_diag.stage=7;
 check=verify_geometry(0);prepare_diag.geometry_code=(uint32_t)check;if(check)return prepare_return(200+check);
 selected=op->compute[0];
 if(warmup){
  for(size_t i=0;i<M/4;i++)atomic_store(&seen[i],0);
  atomic_store(&callback_calls,0);atomic_store(&participant_a,0);atomic_store(&participant_b,0);atomic_store(&coverage_errors,0);
  op->compute[0].task_2d_tile_2d=observed;
 }
 prepare_diag.stage=8;return prepare_return(0);
}
int screen_prepare(unsigned d,unsigned coverage) { return screen_prepare_common(d,coverage,0); }
int screen_prepare_fixture(unsigned d,unsigned coverage,unsigned fixture_id) {
 if(fixture_id<1 || fixture_id>11)return 30;
 return screen_prepare_common(d,coverage,fixture_id);
}
/* This is the sole actual operator invocation. No hidden run in any other export. */
int screen_run(void) { return (int)xnn_run_operator(op,pool); }
int screen_after(void) {
 if(diagnostic){
  op->compute[0]=selected;
  if(atomic_load(&coverage_errors) || atomic_load(&callback_calls)!=M/expected_tile() || !atomic_load(&participant_a) || !atomic_load(&participant_b) || atomic_load(&participant_a)==atomic_load(&participant_b))return 70;
  for(size_t i=0;i<M/expected_tile();i++)if(atomic_load(&seen[i])!=1)return 71;
 }
 return verify_geometry(0);
}
uintptr_t screen_ptr(unsigned i) {
 if(!op)return 0;const struct igemm_context* c=&op->context.igemm.igemm;
 const void* ptrs[]={input,weights,bias,output,c->packed_w,c->indirect_a,c->zero};
 return i<7?(uintptr_t)ptrs[i]:0;
}
size_t screen_bytes(unsigned i) {
 const size_t sizes[]={IO_BYTES+64,RAW_WEIGHT_BYTES,512,IO_BYTES,PACKED_BYTES,INDIRECT_BYTES,ZERO_BYTES};return i<7?sizes[i]:0;
}
/* screen_meta indices 22 onward are a bounded, private failure-only view.
 * Validity guards prevent reading the igemm union before successful reshape.
 * Pointer fields are reduced to presence/equality booleans, never addresses. */
static uint32_t screen_prepare_meta(unsigned i) {
 const unsigned operator_valid=prepare_diag.created && op!=NULL;
 const unsigned context_valid=operator_valid && prepare_diag.reshaped && op->ukernel.type==xnn_microkernel_type_igemm;
 const struct igemm_context* c=context_valid?&op->context.igemm.igemm:NULL;
 const struct compute_parameters* t=context_valid?&op->compute[0]:NULL;
#define DOP(expr) (operator_valid?(uint32_t)(expr):UINT32_MAX)
#define DCTX(expr) (context_valid?(uint32_t)(expr):UINT32_MAX)
 switch(i) {
 case 0:return (uint32_t)(2); /* version */
 case 1:return (uint32_t)(prepare_diag.stage); /* stage */
 case 2:return (uint32_t)(prepare_diag.return_code); /* return_code */
 case 3:return (uint32_t)(prepare_diag.last_api_status); /* last_api_status */
 case 4:return (uint32_t)(prepare_diag.geometry_code); /* geometry_code */
 case 5:return (uint32_t)(prepare_diag.created); /* created */
 case 6:return (uint32_t)(prepare_diag.reshaped); /* reshaped */
 case 7:return (uint32_t)(prepare_diag.setup); /* setup */
 case 8:return (uint32_t)(prepare_diag.requested_dilation); /* requested_dilation */
 case 9:return (uint32_t)(prepare_diag.requested_warmup); /* requested_warmup */
 case 10:return (uint32_t)(prepare_diag.workspace_size); /* workspace_size */
 case 11:return (uint32_t)(prepare_diag.workspace_alignment); /* workspace_alignment */
 case 12:return (uint32_t)(prepare_diag.output_height); /* reshape_output_height */
 case 13:return (uint32_t)(prepare_diag.output_width); /* reshape_output_width */
 case 14:return (uint32_t)(op!=NULL); /* operator_present */
 case 15:return (uint32_t)(pool!=NULL); /* pool_present */
 case 16:return (uint32_t)(pool?(uint32_t)pthreadpool_get_threads_count(pool):0); /* pool_threads */
 case 17:return (uint32_t)(allocation_error); /* allocation_error */
 case 18:return (uint32_t)(input!=NULL); /* caller_input_present */
 case 19:return (uint32_t)(weights!=NULL); /* caller_weights_present */
 case 20:return (uint32_t)(bias!=NULL); /* caller_bias_present */
 case 21:return (uint32_t)(output!=NULL); /* caller_output_present */
 case 22:return (uint32_t)(operator_valid); /* operator_fields_valid */
 case 23:return (uint32_t)(context_valid); /* context_fields_valid */
 case 24:return (uint32_t)(ZERO_BYTES); /* expected_zero_bytes */
 case 25:return (uint32_t)(expected_tile()); /* expected_tile */
 case 26:return (uint32_t)(DOP(op->type)); /* operator_type */
 case 27:return (uint32_t)(DOP(op->ukernel.type)); /* microkernel_type */
 case 28:return (uint32_t)(DOP(op->flags)); /* flags */
 case 29:return (uint32_t)(DOP(op->weights_cache!=NULL)); /* weights_cache_present */
 case 30:return (uint32_t)(DOP(op->batch_size)); /* batch_size */
 case 31:return (uint32_t)(DOP(op->groups)); /* groups */
 case 32:return (uint32_t)(DOP(op->input_height)); /* input_height */
 case 33:return (uint32_t)(DOP(op->input_width)); /* input_width */
 case 34:return (uint32_t)(DOP(op->output_height)); /* output_height */
 case 35:return (uint32_t)(DOP(op->output_width)); /* output_width */
 case 36:return (uint32_t)(DOP(op->group_input_channels)); /* input_channels */
 case 37:return (uint32_t)(DOP(op->group_output_channels)); /* output_channels */
 case 38:return (uint32_t)(DOP(op->input_pixel_stride)); /* input_pixel_stride */
 case 39:return (uint32_t)(DOP(op->output_pixel_stride)); /* output_pixel_stride */
 case 40:return (uint32_t)(DOP(op->kernel_height)); /* kernel_height */
 case 41:return (uint32_t)(DOP(op->kernel_width)); /* kernel_width */
 case 42:return (uint32_t)(DOP(op->stride_height)); /* stride_height */
 case 43:return (uint32_t)(DOP(op->stride_width)); /* stride_width */
 case 44:return (uint32_t)(DOP(op->dilation_height)); /* dilation_height */
 case 45:return (uint32_t)(DOP(op->dilation_width)); /* dilation_width */
 case 46:return (uint32_t)(DOP(op->padding_top)); /* padding_top */
 case 47:return (uint32_t)(DOP(op->padding_right)); /* padding_right */
 case 48:return (uint32_t)(DOP(op->padding_bottom)); /* padding_bottom */
 case 49:return (uint32_t)(DOP(op->padding_left)); /* padding_left */
 case 50:return (uint32_t)(DOP(op->zero_size)); /* operator_zero_size */
 case 51:return (uint32_t)(DOP(op->state)); /* operator_state */
 case 52:return (uint32_t)(DOP(op->quantization_params!=NULL)); /* operator_quantization_present */
 case 53:return (uint32_t)(DCTX(op->ukernel.igemm.mr)); /* mr */
 case 54:return (uint32_t)(DCTX(op->ukernel.igemm.nr)); /* nr */
 case 55:return (uint32_t)(DCTX(op->ukernel.igemm.kr)); /* kr */
 case 56:return (uint32_t)(DCTX(op->ukernel.igemm.sr)); /* sr */
 case 57:return (uint32_t)(DCTX(c->ukernel.function[0]==(xnn_igemm_ukernel_fn)xnn_f32_igemm_ukernel_4x8__wasmsimd_loadsplat)); /* kernel_expected */
 case 58:return (uint32_t)(DCTX(op->ukernel.igemm.igemm_cases[3].function[0]==c->ukernel.function[0])); /* kernel_matches_case3 */
 case 59:return (uint32_t)(DCTX(t->type)); /* task_type */
 case 60:return (uint32_t)(DCTX(t->task_2d_tile_2d==expected_callback())); /* task_expected */
 case 61:return (uint32_t)(DCTX(t->context_offset)); /* context_offset */
 case 62:return (uint32_t)(DCTX(t->range[0])); /* range0 */
 case 63:return (uint32_t)(DCTX(t->range[1])); /* range1 */
 case 64:return (uint32_t)(DCTX(t->range[2])); /* range2 */
 case 65:return (uint32_t)(DCTX(t->range[3])); /* range3 */
 case 66:return (uint32_t)(DCTX(t->range[4])); /* range4 */
 case 67:return (uint32_t)(DCTX(t->range[5])); /* range5 */
 case 68:return (uint32_t)(DCTX(t->tile[0])); /* tile0 */
 case 69:return (uint32_t)(DCTX(t->tile[1])); /* tile1 */
 case 70:return (uint32_t)(DCTX(op->compute[1].type)); /* compute1_type */
 case 71:return (uint32_t)(DCTX(op->compute[2].type)); /* compute2_type */
 case 72:return (uint32_t)(DCTX(c->zero_size)); /* context_zero_size */
 case 73:return (uint32_t)(DCTX(c->zero_buffers!=NULL)); /* zero_buffers_present */
 case 74:return (uint32_t)(DCTX(c->quantization_params!=NULL)); /* context_quantization_present */
 case 75:return (uint32_t)(DCTX(c->indirect_a!=NULL)); /* indirection_present */
 case 76:return (uint32_t)(DCTX(c->zero!=NULL)); /* zero_present */
 case 77:return (uint32_t)(DCTX(c->packed_w!=NULL)); /* packed_present */
 case 78:return (uint32_t)(DCTX(c->indirect_a==op->indirection_buffer)); /* indirection_matches */
 case 79:return (uint32_t)(DCTX(c->zero==op->zero_buffer)); /* zero_matches */
 case 80:return (uint32_t)(DCTX(c->packed_w==op->packed_weights.pointer)); /* packed_matches */
 case 81:return (uint32_t)(DCTX(c->c==NULL)); /* output_null */
 case 82:return (uint32_t)(DCTX(c->c==output)); /* output_matches */
 case 83:return (uint32_t)(DCTX(c->a_offset==0)); /* a_offset_zero */
 case 84:return (uint32_t)(DCTX(c->ks)); /* ks */
 case 85:return (uint32_t)(DCTX(c->ks_scaled)); /* ks_scaled */
 case 86:return (uint32_t)(DCTX(c->kc)); /* kc */
 case 87:return (uint32_t)(DCTX(c->w_stride)); /* w_stride */
 case 88:return (uint32_t)(DCTX(c->cm_stride)); /* cm_stride */
 case 89:return (uint32_t)(DCTX(c->cn_stride)); /* cn_stride */
 case 90:return (uint32_t)(DCTX(c->ga_stride)); /* ga_stride */
 case 91:return (uint32_t)(DCTX(c->gw_stride)); /* gw_stride */
 case 92:return (uint32_t)(DCTX(c->gc_stride)); /* gc_stride */
 case 93:return (uint32_t)(DCTX(c->ba_stride)); /* ba_stride */
 case 94:return (uint32_t)(DCTX(c->bc_stride)); /* bc_stride */
 case 95:return (uint32_t)(DCTX(c->log2_csize)); /* log2_csize */
 case 96:return (uint32_t)(lifecycle_diag.initialize_called); /* initialize_called */
 case 97:return (uint32_t)(lifecycle_diag.initialize_status); /* initialize_status */
 case 98:return (uint32_t)(lifecycle_diag.initialize_return_code); /* initialize_return_code */
 case 99:return (uint32_t)(lifecycle_diag.library_initialized); /* library_initialized */
 case 100:return (uint32_t)(lifecycle_diag.pool_created); /* pool_created */
 case 101:return (uint32_t)(lifecycle_diag.pool_destroy_invoked); /* pool_destroy_invoked */
 case 102:return (uint32_t)(lifecycle_diag.pool_destroy_returned); /* pool_destroy_returned */
 case 103:return (uint32_t)(lifecycle_diag.deinitialize_invoked); /* deinitialize_invoked */
 case 104:return (uint32_t)(lifecycle_diag.deinitialize_status); /* deinitialize_status */
 default:return UINT32_MAX;
 }
#undef DOP
#undef DCTX
}
size_t screen_meta(unsigned i) {
 if(i>=22)return screen_prepare_meta(i-22);
 if(!op)return 0;const struct igemm_context* c=&op->context.igemm.igemm;
 const size_t values[]={dilation,M,128,4,8,11,512,176,expected_tile(),128,pthreadpool_get_threads_count(pool),selected.range[0],selected.range[1],c->a_offset,(uintptr_t)op,(uintptr_t)pool,(uintptr_t)selected.task_2d_tile_2d,(uintptr_t)c->ukernel.function[0],(size_t)atomic_load(&participant_a),(size_t)atomic_load(&participant_b),(size_t)atomic_load(&callback_calls),diagnostic};
 return i<sizeof(values)/sizeof(values[0])?values[i]:0;
}
int screen_delete(void) {
 int bad=screen_canaries();
 if(op){enum xnn_status s=xnn_delete_operator(op);op=NULL;if(s!=xnn_status_success)bad=80+(int)s;}
 dealloc(NULL,input);dealloc(NULL,weights);dealloc(NULL,bias);dealloc(NULL,output);input=weights=bias=output=NULL;
 for(unsigned i=0;i<MAX_ALLOCS;i++)if(allocations[i].raw)bad=92;
 return bad?bad:screen_canaries();
}
int screen_shutdown(void) {
 if(op)return 90;
 if(pool){lifecycle_diag.pool_destroy_invoked=1;pthreadpool_destroy(pool);lifecycle_diag.pool_destroy_returned=1;pool=NULL;}
 if(lifecycle_diag.library_initialized){
  lifecycle_diag.deinitialize_invoked=1;
  enum xnn_status status=xnn_deinitialize();lifecycle_diag.deinitialize_status=(uint32_t)status;
  if(status!=xnn_status_success)return 91;
  lifecycle_diag.library_initialized=0;
 }
 return screen_canaries();
}
