"""Lightweight tests: no downloads, compilers, models, inference, or browser."""
import hashlib,json,tempfile,unittest
from pathlib import Path
import prepare_vocoder_runtime as p

def ar(members):
    data=b'!<arch>\n'
    for name,payload in members:
        header=f'{name+"/":<16}{0:<12}{0:<6}{0:<6}{644:<8}{len(payload):<10}`\n'.encode()
        assert len(header)==60
        data+=header+payload+(b'\n' if len(payload)&1 else b'')
    return data

def leb(n):
    out=[]
    while True:
        part=n&127;n>>=7;out.append(part|(128 if n else 0))
        if not n:return bytes(out)

class Tests(unittest.TestCase):
    def test_ordered_archive_invariant(self):
        with tempfile.TemporaryDirectory() as td:
            a=Path(td)/'a.a';b=Path(td)/'b.a'
            original=[('gemm-config.c.o',b'old'),('pthreadpool.o',b'actual pool'),('same.o',b'a'),('same.o',b'b')]
            a.write_bytes(ar(original));baseline=p.archive_members(a)
            b.write_bytes(ar([(name,b'new' if name=='gemm-config.c.o' else payload) for name,payload in original]))
            p.verify_archive_change(baseline,b)
            self.assertEqual(len(baseline),4)
            b.write_bytes(ar([(name,b'wrong' if name=='pthreadpool.o' else payload) for name,payload in original]))
            with self.assertRaises(ValueError):p.verify_archive_change(baseline,b)
            with self.assertRaises(ValueError):p.verify_archive_change(baseline,a)
    def test_export_mapping(self):
        required=['bench_init','bench_synthesize','bench_raw','bench_raw_ptr','bench_raw_len','bench_fixed_length','bench_fixed_matches','bench_xnn_threads','bench_xnn_sessions','bench_finish_profile','probe_xnn_f32_dispatch']
        with tempfile.TemporaryDirectory() as td:
            js=Path(td)/'x.js';payload=leb(len(required));lines=[]
            for i,name in enumerate(required):
                key='x'+str(i);raw=key.encode();payload+=leb(len(raw))+raw+b'\0'+leb(i);lines.append(f'Module["_{name}"]=wasmExports["{key}"];')
            js.write_text('\n'.join(lines));js.with_suffix('.wasm').write_bytes(b'\0asm\1\0\0\0'+b'\7'+leb(len(payload))+payload)
            self.assertEqual(len(p.verify_exports(js)),len(required))
            js.write_text('\n'.join(lines[:-1]))
            with self.assertRaises(ValueError):p.verify_exports(js)
    def test_pin_rejection(self):
        with tempfile.TemporaryDirectory() as td:
            src=Path(td)/'bad.py';src.write_text('print("not the pinned harness")')
            with self.assertRaises(ValueError):p.generate_builder(src,Path(td)/'out.py')
    def test_repair_contract(self):
        self.assertIn('"-p", "voicevox_core", "-p", "voicevox_benchmark"',p.BUILD_AND_VERIFY)
        self.assertIn('--extern voicevox_core=',p.BUILD_AND_VERIFY)
        self.assertIn('intended != bundled',p.BUILD_AND_VERIFY)
        self.assertNotIn('/workspace',Path(p.__file__).read_text())

if __name__=='__main__':unittest.main()
