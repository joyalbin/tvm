# pylint: disable=invalid-name,unused-variable
"""Depthwise convolution schedule for ARM CPU"""

import tvm
from tvm import autotvm

from ..generic import schedule_depthwise_conv2d_nchw
from ..nn import depthwise_conv2d_nchw
from ..util import traverse_inline

# register original implementation of depthwise_conv2d_nchw since we don't need to change this part
autotvm.task.register_topi_compute(depthwise_conv2d_nchw, 'arm_cpu', 'direct',
                                   depthwise_conv2d_nchw.fdefault)

# register customized schedule for arm cpu.
@autotvm.task.register_topi_schedule(schedule_depthwise_conv2d_nchw, 'arm_cpu', 'direct')
def schedule_depthwise_conv2d_nchw_(cfg, outs):
    """Schedule depthwise conv2d"""
    outs = [outs] if isinstance(outs, tvm.tensor.Tensor) else outs
    s = tvm.create_schedule([x.op for x in outs])

    def _schedule(cfg, s, data, data_pad, kernel, output):
        A, B, C = data, kernel, output
        s[data_pad].compute_inline()

        # define tile
        n, c, h, w = s[output].op.axis
        cfg.define_split('tile_c', c, num_outputs=2)
        cfg.define_split('tile_h', h, num_outputs=2)
        cfg.define_split('tile_w', w, num_outputs=2)

        # park data to vector form  [n, c, h, w] -> [n, C, h, w, VC]
        A0 = s.cache_read(data_pad, "global", C)
        _, c, h, w = s[A0].op.axis
        c, vc = cfg['tile_c'].apply(s, A0, c)
        s[A0].reorder(c, h, w, vc)
        A1 = s.cache_write(A0, 'global')
        s[A0].compute_inline()

        # park kernel to vector form  [co, ci, kh, kw] -> [CO, ci, kh, kw, VC]
        B0 = s.cache_read(B, "global", C)
        c, m, h, w = s[B0].op.axis
        c, vc, = cfg['tile_c'].apply(s, B0, c)
        s[B0].reorder(c, m, h, w, vc)
        B1 = s.cache_write(B0, 'global')
        s[B0].compute_inline()

        _, c, h, w = s[C].op.axis
        c, vc, = cfg['tile_c'].apply(s, C, c)
        s[C].reorder(c, h, w, vc)

        # depthwise conv
        C0 = s.cache_write(C, 'global')
        _, c, h, w, vc = s[C0].op.axis
        dh, dw = s[C0].op.reduce_axis
        oh, ih = cfg['tile_h'].apply(s, C0, h)
        ow, iw = cfg['tile_w'].apply(s, C0, w)
        s[C0].reorder(c, oh, ow, dh, dw, ih, iw, vc)
        s[A1].compute_at(s[C0], oh)

        # try unroll and vectorization
        cfg.define_annotate('ann', [ih, iw, vc], policy='try_unroll_vec')
        cfg['ann'].apply(s, C0, [ih, iw, vc],
                         axis_lens=[cfg['tile_h'].size[-1],
                                    cfg['tile_w'].size[-1],
                                    cfg['tile_c'].size[-1]],
                         max_unroll=16,
                         cfg=cfg)

        # mark parallel
        n, c, h, w = s[C].op.axis
        s[C].parallel(c)

        n, c, h, w, vc = s[C0].op.axis
        s[C0].parallel(c)

        c, m, h, w, vc = s[B1].op.axis
        s[B1].parallel(c)

        return s

    scheduled_ops = []

    def _callback(op):
        if op.tag == 'depthwise_conv2d_nchw' and op not in scheduled_ops:
            output = op.output(0)
            kernel = op.input_tensors[1]
            data = op.input_tensors[0]
            data_pad = None
            if isinstance(data.op, tvm.tensor.ComputeOp) and "pad" in data.op.tag:
                data_pad = data
                data = data_pad.op.input_tensors[0]
            _schedule(cfg, s, data, data_pad, kernel, output)

        scheduled_ops.append(op)

    traverse_inline(s, outs[0].op, _callback)
    return s
