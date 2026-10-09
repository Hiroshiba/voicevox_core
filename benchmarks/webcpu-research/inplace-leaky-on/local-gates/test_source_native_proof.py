import copy,json,unittest
import source_native_proof as n


def fixture(variant='candidate',width='xmm',function_index=8227):
    rows=[]
    def row(off,op,args='',target=None):rows.append({'offset':off,'mnemonic':op,'operands':args.split(',') if args else [],'target':target})
    row(0,'movl','r8,[r15+rax*1+0x8]');row(5,'movl','r9,[r15+rax*1+0xc]')
    row(10,'movl','r10,[r15+rdx*1]');row(15,'movl','r11,[r15+rcx*1]');row(20,'subl','r11,r10')
    row(25,'vmovss',f'{width}0,[r15+rax*1+0x10]');row(30,'vbroadcastss',f'{width}0,{width}0');row(35,'vxorps',f'{width}1,{width}1,{width}1')
    if variant=='candidate':
        row(40,'cmpl','r8,r9');row(45,'jnz',target=65);row(50,'cmpl','r11,0x4');row(55,'jl',target=150);row(60,'jmp',target=100)
    else:row(40,'jmp',target=65)
    row(65,'cmpl','r11,0x4');row(70,'jl',target=150);row(75,'movl','r12,r9');row(80,'subl','r12,r8');row(85,'cmpl','r12,0x10');row(90,'jb',target=150);row(95,'jmp',target=100)
    row(100,'vmovups',f'{width}2,[r15+r8*1]');row(105,'vmulps',f'{width}3,{width}0,{width}2');row(110,'vcmpps',f'{width}4,{width}1,{width}2,0x2');row(115,'vpandn',f'{width}5,{width}4,{width}3');row(120,'vpand',f'{width}6,{width}4,{width}2');row(125,'vpor',f'{width}7,{width}5,{width}6');row(130,'vmovups',f'[r15+r9*1],{width}7');row(135,'ret')
    row(150,'vmovss',f'{width}2,[r15+r8*1]');row(155,'vmulss',f'{width}3,{width}0,{width}2');row(160,'vmovss',f'[r15+r9*1],{width}3');row(165,'ret')
    return rows


def dump(rows,index=8227,compiler='TurboFan',base=0x10000000):
    text=f'--- WebAssembly code ---\nname: wasm-function[{index}]\nindex: {index}\nkind: wasm function\ncompiler: {compiler}\nBody (size = 192 = 170 + 22 padding)\nInstructions (size = 170)\n'
    byoffset={x['offset']:x for x in rows}
    rows=[byoffset.get(i,{'offset':i,'mnemonic':'nop','operands':[],'target':None}) for i in range(0,170,5)]
    for row in rows:
        args=f"0x{base+row['target']:x}" if row['target'] is not None else ','.join(row['operands'])
        text+=f"0x{base+row['offset']:x} {row['offset']:x} 9090909090 {row['mnemonic']} {args}\n"
    return text+'\nSafepoints (size = 0)\n--- End code ---\n'


class NativeProofTests(unittest.TestCase):
    def test_three_variants_and_128_256(self):
        for variant in ['original','rebuilt','candidate']:
            for width in ['xmm','ymm']:
                proof=n.parse_native_dump(dump(fixture(variant,width)),8227,variant)
                self.assertTrue(n.validate_native_proof(json.loads(json.dumps(proof)),variant,8227))
    def test_wrong_equality_branch_rejected(self):
        rows=fixture();next(x for x in rows if x['offset']==45)['mnemonic']='jz'
        with self.assertRaises(n.NativeProofError):n.parse_native_dump(dump(rows),8227,'candidate')
    def test_wrong_compare_direction_rejected(self):
        rows=fixture();next(x for x in rows if x['offset']==110)['operands']=['xmm4','xmm2','xmm1','0x2']
        with self.assertRaises(n.NativeProofError):n.parse_native_dump(dump(rows),8227,'candidate')
    def test_disconnected_simd_rejected(self):
        rows=fixture();next(x for x in rows if x['offset']==60)['target']=150
        with self.assertRaises(n.NativeProofError):n.parse_native_dump(dump(rows),8227,'candidate')
    def test_missing_original_fallback_rejected(self):
        rows=fixture();next(x for x in rows if x['offset']==45)['target']=100
        with self.assertRaises(n.NativeProofError):n.parse_native_dump(dump(rows),8227,'candidate')
    def test_wrong_function_binding_rejected(self):
        with self.assertRaises(n.NativeProofError):n.parse_native_dump(dump(fixture(),8228),8227,'candidate')
    def test_fma_rejected(self):
        rows=fixture();next(x for x in rows if x['offset']==105)['mnemonic']='vfmadd132ps'
        with self.assertRaises(n.NativeProofError):n.parse_native_dump(dump(rows),8227,'candidate')
    def test_wrong_selected_value_rejected(self):
        rows=fixture();next(x for x in rows if x['offset']==130)['operands'][1]='xmm3'
        with self.assertRaises(n.NativeProofError):n.parse_native_dump(dump(rows),8227,'candidate')
    def test_manual_findings_flag_not_trusted(self):
        proof=n.parse_native_dump(dump(fixture()),8227,'candidate');proof['evidence']['findings']['no_fma']=False
        with self.assertRaises(n.NativeProofError):n.validate_native_proof(proof,'candidate',8227)
    def test_multiple_instances_must_agree(self):
        text=dump(fixture(),base=0x10000000)+dump(fixture(),base=0x20000000)
        self.assertEqual(n.parse_native_dump(text,8227,'candidate')['optimized_blocks'],2)
        rows=fixture();next(x for x in rows if x['offset']==35)['mnemonic']='vpxor'
        with self.assertRaises(n.NativeProofError):n.parse_native_dump(text+dump(rows),8227,'candidate')
    def test_unparsed_instruction_line_stops(self):
        text=dump(fixture()).replace('9090909090 vmulps','9090909090 ???vmulps')
        with self.assertRaises(n.NativeProofError):n.parse_native_dump(text,8227,'candidate')
    def test_missing_instruction_bytes_stop(self):
        text=dump(fixture()).replace('9090909090 vmulps','9090 vmulps')
        with self.assertRaises(n.NativeProofError):n.parse_native_dump(text,8227,'candidate')
    def test_unsupported_instruction_stops(self):
        rows=fixture();next(x for x in rows if x['offset']==105)['mnemonic']='unsupported'
        with self.assertRaises(n.NativeProofError):n.parse_native_dump(dump(rows),8227,'candidate')
    def test_call_clobber_not_silently_preserved(self):
        rows=fixture();next(x for x in rows if x['offset']==35).update(mnemonic='call',operands=['0x1234'])
        with self.assertRaises(n.NativeProofError):n.parse_native_dump(dump(rows),8227,'candidate')
    def test_bounded_failure_never_exports_message(self):
        value=n.native_failure_evidence(n.NativeProofError('private machine path secret'))
        self.assertTrue(n.validate_native_failure_evidence(value))
        self.assertNotIn('private',json.dumps(value))
    def test_liftoff_only_stops(self):
        with self.assertRaises(n.NativeProofError):n.parse_native_dump(dump(fixture(),compiler='Liftoff'),8227,'candidate')


if __name__=='__main__':unittest.main()
