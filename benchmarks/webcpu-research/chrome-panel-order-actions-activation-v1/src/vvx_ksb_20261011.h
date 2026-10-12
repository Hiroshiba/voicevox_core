// Private source-only KS-blocking experiment. BSD-style XNNPACK-derived code.
#pragma once
#include <stddef.h>
#include <stdbool.h>
#include "xnnpack/common.h"
#include "xnnpack/compute.h"
struct xnn_operator;
#if !defined(VVX_KSB_ARM) || (VVX_KSB_ARM != 1 && VVX_KSB_ARM != 2 && VVX_KSB_ARM != 3)
#error "Set VVX_KSB_ARM to 1 (B), 2 (P), or 3 (Q), only in the isolated build."
#endif
XNN_PRIVATE void vvx_ksb_20261011_select(struct xnn_operator*, size_t, size_t,
    size_t, size_t, size_t, bool, uint32_t, uint32_t, uint32_t, uint32_t);
XNN_PRIVATE void vvx_ksb_20261011_B(
    const struct igemm_context context[restrict XNN_MIN_ELEMENTS(1)],
    size_t m_start, size_t n_start, size_t m_size, size_t n_size);
XNN_PRIVATE void vvx_ksb_20261011_P(
    const struct igemm_context context[restrict XNN_MIN_ELEMENTS(1)],
    size_t m_start, size_t n_start, size_t m_size, size_t n_size);
XNN_PRIVATE void vvx_ksb_20261011_Q(
    const struct igemm_context context[restrict XNN_MIN_ELEMENTS(1)],
    size_t m_start, size_t n_start, size_t m_size, size_t n_size);
