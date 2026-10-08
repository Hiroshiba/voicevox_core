from pathlib import Path
import difflib,os
P=Path(__file__).parent;X=Path(os.environ['XNN_ROOT'])
wrapper='''
void xnn_compute_igemm_m8(const struct igemm_context* context,
    size_t m, size_t n, size_t mc, size_t nc)
{
  const size_t mr = context->ks_scaled / (context->ks * sizeof(void*));
  assert(context->ks_scaled % (context->ks * sizeof(void*)) == 0 && mr == 4);
  for (size_t i = 0; i < mc; i += mr) {
    xnn_compute_igemm(context, m + i, n, min(mr, mc - i), nc);
  }
}

void xnn_compute_igemm_m8_nc32(const struct igemm_context* context,
    size_t m, size_t n, size_t mc, size_t nc)
{
  const size_t mr = context->ks_scaled / (context->ks * sizeof(void*));
  const size_t nr = context->cn_stride >> context->log2_csize;
  assert(context->ks_scaled % (context->ks * sizeof(void*)) == 0 && mr == 4);
  assert(nr == 8 && context->log2_csize == 2);
  for (size_t j = 0; j < nc; j += 4 * nr) {
    for (size_t i = 0; i < mc; i += mr) {
      xnn_compute_igemm(context, m + i, n + j, min(mr, mc - i), min(4 * nr, nc - j));
    }
  }
}
'''
selector='''
#if XNN_MACRO_CONDITION < 0 || XNN_MACRO_CONDITION > 2
#error Invalid XNN_MACRO_CONDITION
#endif
#if XNN_ARCH_WASMSIMD && !XNN_ARCH_WASMRELAXEDSIMD && XNN_MACRO_CONDITION != 0
  if (convolution_op->type == xnn_operator_type_convolution_nhwc_f32 &&
      !dynamic_quantization && batch_size == 1 && groups == 1 &&
      input_height == 1 && output_height == 1 && kernel_height == 1 &&
      output_width == input_width && convolution_op->stride_height == 1 && convolution_op->dilation_height == 1 &&
      convolution_op->padding_top == 0 && convolution_op->padding_bottom == 0 &&
      convolution_op->padding_left == 5 * convolution_op->dilation_width &&
      convolution_op->padding_right == 5 * convolution_op->dilation_width &&
      group_input_channels == 128 && group_output_channels == 128 &&
      convolution_op->input_pixel_stride == 128 && convolution_op->output_pixel_stride == 128 &&
      kernel_width == 11 && convolution_op->stride_width == 1 &&
      (convolution_op->dilation_width == 1 || convolution_op->dilation_width == 3 || convolution_op->dilation_width == 5) &&
      mr == 4 && nr == 8 && output_size >= 32 &&
      !(convolution_op->flags & XNN_FLAG_TRANSIENT_INDIRECTION_BUFFER) &&
      convolution_op->compute[igemm_compute_index].type == xnn_parallelization_type_2d_tile_2d &&
      convolution_op->compute[igemm_compute_index].task_2d_tile_2d == (pthreadpool_task_2d_tile_2d_t) xnn_compute_igemm)
  {
    convolution_op->compute[igemm_compute_index].task_2d_tile_2d =
      (pthreadpool_task_2d_tile_2d_t) (XNN_MACRO_CONDITION == 1 ? xnn_compute_igemm_m8 : xnn_compute_igemm_m8_nc32);
    convolution_op->compute[igemm_compute_index].tile[0] = 32;
    convolution_op->compute[igemm_compute_index].tile[1] = 128;
  }
#endif
'''
diffs=[]
for rel in ['src/operator-run.c','src/operators/convolution-nhwc.c','src/xnnpack/compute.h']:
 orig=(X/rel).read_text();s=orig
 if rel.endswith('operator-run.c'):s=s.replace('\nvoid xnn_compute_dqigemm(',wrapper+'\nvoid xnn_compute_dqigemm(',1)
 elif rel.endswith('convolution-nhwc.c'):
  start=s.index('static enum xnn_status reshape_igemm(');end=s.index('static enum xnn_status reshape_dwconv(',start)
  part=s[start:end];needle='  convolution_op->state = xnn_run_state_needs_setup;';assert part.count(needle)==1
  s=s[:start]+part.replace(needle,selector+needle)+s[end:]
 else:
  needle='XNN_PRIVATE void xnn_compute_dqigemm(';assert needle in s
  decl='XNN_PRIVATE void xnn_compute_igemm_m8(const struct igemm_context*, size_t, size_t, size_t, size_t);\nXNN_PRIVATE void xnn_compute_igemm_m8_nc32(const struct igemm_context*, size_t, size_t, size_t, size_t);\n\n'
  s=s.replace(needle,decl+needle,1)
 dest=P/'sources'/rel;dest.parent.mkdir(parents=True,exist_ok=True);dest.write_text(s)
 diffs.extend(difflib.unified_diff(orig.splitlines(True),s.splitlines(True),fromfile='a/'+rel,tofile='b/'+rel))
(P/'results/production.patch').write_text(''.join(diffs))
