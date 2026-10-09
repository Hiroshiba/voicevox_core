"""Conservative x64 V8 native guard/dataflow proof. Unsupported lowering stops.

This is a deliberately narrow verifier, not a general x86 emulator. Evidence is
bounded normalized instruction text; validation recomputes CFG/dataflow results.
The caller must bind the dump to the actual module and observed callback metadata.
"""
from __future__ import annotations
import copy
import hashlib
import json
import re

ABI_SOURCE = 'https://raw.githubusercontent.com/nodejs/node/v24.19.0/deps/v8/src/wasm/wasm-linkage.h'
SCENARIOS = ('exact_ge4', 'disjoint_ge4', 'overlap_after_ge4', 'exact_short')
MAX_INSTRUCTIONS = 2400
MAX_STATES = 10000

CONDITION_CODES = frozenset('e ne z nz b be a ae l le g ge c nc nae nb na nbe nge nl ng nle p np s ns o no'.split())
SUPPORTED_OPS = frozenset(('ret retq retl ud2 int3 jmp jmpq '
 'movl movq movw movb movzxbl movzxwl movsxlq movzxbq movzxwq leal leaq '
 'addl addq subl subq andl andq orl orq xorl xorq imull imulq shll shlq sall salq '
 'cmpl cmpq cmpw cmpb testl testq testw testb '
 'vxorps vpxor xorps pxor vbroadcastss vpbroadcastd vshufps vpshufd shufps pshufd '
 'vmovups vmovaps vmovdqu vmovdqa movups movaps movdqu movdqa vmovss movss vmovd movd '
 'vmulps mulps vmulss mulss vcmpps cmpps vcmpleps vcmpltps vcmpgeps '
 'vpandn vandnps pandn andnps vpand vandps pand andps vpor vorps por orps vblendvps '
 'call callq nop nopl nopw push pushq pop popq vzeroupper ucomiss vucomiss').split())


class NativeProofError(ValueError):
    pass


def need(ok, message):
    if not ok:
        raise NativeProofError(message)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def gp(name):
    name = name.strip().lower().lstrip('%')
    aliases = {'eax':'rax','ax':'rax','al':'rax','edx':'rdx','dx':'rdx','dl':'rdx',
               'ecx':'rcx','cx':'rcx','cl':'rcx','ebx':'rbx','bx':'rbx','bl':'rbx',
               'esi':'rsi','si':'rsi','sil':'rsi','edi':'rdi','di':'rdi','dil':'rdi',
               'esp':'rsp','sp':'rsp','spl':'rsp','ebp':'rbp','bp':'rbp','bpl':'rbp'}
    if name in aliases:
        return aliases[name]
    match = re.fullmatch(r'(r\d+)[dlwb]?', name)
    return match[1] if match else name


def imm(text):
    text = text.strip()
    return int(text, 16 if '0x' in text else 10)


def add(a, b):
    if a == 0: return b
    if b == 0: return a
    if isinstance(a, int) and isinstance(b, int): return a+b
    return ('add', a, b)


def sub(a, b):
    if a == b: return 0
    if a == ('E',) and b == ('B',): return ('N',)
    if b == 0: return a
    if isinstance(a, int) and isinstance(b, int): return a-b
    return ('sub', a, b)


def terms(expr):
    if isinstance(expr, tuple) and expr and expr[0] == 'add':
        return terms(expr[1]) + terms(expr[2])
    return [expr]


def has(expr, tag):
    if expr == (tag,): return True
    return isinstance(expr, tuple) and any(has(x, tag) for x in expr[1:])


def interval(expr, scenario):
    if isinstance(expr, int): return (expr, expr)
    if expr == ('N',): return (0, 3) if scenario == 'exact_short' else (4, 0x1fffffff)
    if expr == ('sub', ('O',), ('I',)):
        return (0, 0) if scenario.startswith('exact') else (1,15) if scenario == 'overlap_after_ge4' else (16,0xffffffff)
    if expr == ('sub', ('I',), ('O',)):
        r = interval(('sub', ('O',), ('I',)), scenario)
        return (-r[1], -r[0])
    if isinstance(expr, tuple) and expr:
        if expr[0] == 'bool':
            value = condition(expr[1], expr[2], expr[3], scenario)
            return (int(value), int(value)) if value is not None else (0,1)
        if expr[0] in ('or','and'):
            a,b = interval(expr[1],scenario), interval(expr[2],scenario)
            if a and b:
                if expr[0]=='or' and a[1]<=1 and b[1]<=1:
                    if a[0]==1 or b[0]==1:return (1,1)
                    return (0, max(a[1],b[1]))
                if expr[0]=='and':
                    if a[1]<=1 and b[1]<=1:return (a[0]&b[0], a[1]&b[1])
                    if b[0]==b[1] and b[0] in (-4,0xfffffffc,0x7ffffffc) and a[0]>=0:
                        return (a[0]&b[0],a[1]&b[0])
                    if b[0]==b[1] and b[0] in (255,0xffffffff) and a[1]<=1:return a
        if expr[0] in ('add','sub','mul'):
            a,b=interval(expr[1],scenario),interval(expr[2],scenario)
            if a and b:
                if expr[0]=='add':return (a[0]+b[0],a[1]+b[1])
                if expr[0]=='sub':return (a[0]-b[1],a[1]-b[0])
                vals=[x*y for x in a for y in b];return min(vals),max(vals)
    return None


def condition(cc, a, b, scenario):
    cc={'z':'e','nz':'ne','c':'b','nae':'b','nb':'ae','nc':'ae','na':'be','nbe':'a','nge':'l','nl':'ge','ng':'le','nle':'g'}.get(cc,cc)
    if a == b:
        return cc in ('e','ae','be','ge','le')
    # Relations between pointer bases are known under each alias scenario.
    if a in (('I',),('O',)) and b in (('I',),('O',)) and a!=b:
        if cc in ('e','ne'):
            equal=scenario.startswith('exact');return equal if cc=='e' else not equal
    # Comparing end with begin is comparing the signed range length with zero.
    if a==('E',) and b==('B',):a,b=('N',),0
    elif a==('B',) and b==('E',):a,b=0,('N',)
    ia,ib=interval(a,scenario),interval(b,scenario)
    if not ia or not ib:return None
    if cc=='e':
        if ia[0]==ia[1]==ib[0]==ib[1]:return True
        if ia[1]<ib[0] or ib[1]<ia[0]:return False
        return None
    if cc=='ne':
        v=condition('e',a,b,scenario);return None if v is None else not v
    if cc in ('b','be','a','ae'):
        if ia[0]<0 or ib[0]<0:return None
        cc={'b':'l','be':'le','a':'g','ae':'ge'}[cc]
    if cc=='l':
        if ia[1]<ib[0]:return True
        if ia[0]>=ib[1]:return False
    elif cc=='le':
        if ia[1]<=ib[0]:return True
        if ia[0]>ib[1]:return False
    elif cc=='g':return condition('l',b,a,scenario)
    elif cc=='ge':return condition('le',b,a,scenario)
    return None


def memory_address(text, regs):
    match=re.search(r'\[([^]]+)\]',text)
    if not match:return None
    expr=0
    for sign,part in re.findall(r'([+-]?)([^+-]+)',match[1].replace(' ','')):
        factor=1
        if '*' in part:
            part,f=part.split('*');factor=int(f)
        try:value=imm(part)
        except ValueError:value=regs.get(gp(part),('unknown',gp(part)))
        if factor!=1:value=('mul',value,factor)
        if sign=='-':expr=sub(expr,value)
        else:expr=add(expr,value)
    return expr


def memory_value(operand, regs, spill):
    if 'rbp' in operand or 'rsp' in operand:
        return spill.get(operand,('unknown_stack',operand))
    address=memory_address(operand,regs)
    if address is None:return ('unknown_memory',)
    items=terms(address);constant=sum(x for x in items if isinstance(x,int));symbolic=[x for x in items if not isinstance(x,int)]
    for key,mapping in [('C',{8:('I',),12:('O',),16:('A',)}),('BP',{0:('B',)}),('EP',{0:('E',)})]:
        if symbolic.count((key,))==1 and len(symbolic)<=2 and constant in mapping:
            return mapping[constant]
    if has(address,'I') or has(address,'O'):
        return ('X',address)
    return ('unknown_memory',repr(address))


def operand_value(text, regs, spill):
    if '[' in text:return memory_value(text,regs,spill)
    try:return imm(text)
    except ValueError:return regs.get(gp(text),('unknown',gp(text)))


def vector_select(op, values):
    if op=='mul':
        a,b=values
        if a==('A',) and isinstance(b,tuple) and b[0]=='X':return ('product',b)
        if b==('A',) and isinstance(a,tuple) and a[0]=='X':return ('product',a)
    if op=='andnot':
        a,b=values
        if isinstance(a,tuple) and a[0]=='ge0' and b==('product',a[1]):return ('negative_product',a[1])
    if op=='and':
        a,b=values
        for mask,x in [(a,b),(b,a)]:
            if isinstance(mask,tuple) and mask[0]=='ge0' and x==mask[1]:return ('nonnegative_x',x)
    if op=='or':
        a,b=values
        for pos,neg in [(a,b),(b,a)]:
            if isinstance(pos,tuple) and pos[0]=='nonnegative_x' and neg==('negative_product',pos[1]):return ('selected',pos[1])
    if op=='blend':
        product,x,mask=values
        if mask==('ge0',x) and product==('product',x):return ('selected',x)
    return ('unknown_vector',op)


def analyze_instructions(instructions, variant):
    need(variant in ('original','rebuilt','candidate'),'Unknown variant')
    need(1<=len(instructions)<=MAX_INSTRUCTIONS,'Native instruction count unsupported')
    need(all(set(x)=={'offset','size','mnemonic','operands','target'} for x in instructions),'Native instruction evidence schema')
    for ins in instructions:
        need(type(ins['offset']) is int and 0<=ins['offset']<=1000000, 'Invalid native offset')
        need(isinstance(ins['mnemonic'],str) and len(ins['mnemonic'])<=32, 'Invalid native mnemonic')
        op=ins['mnemonic']
        need(op in SUPPORTED_OPS or op.startswith('j') and op[1:] in CONDITION_CODES or op.startswith('set') and op[3:] in CONDITION_CODES, 'Unsupported native instruction: '+op)
        need(isinstance(ins['operands'],list) and len(ins['operands'])<=4 and all(isinstance(a,str) and len(a)<=200 for a in ins['operands']), 'Invalid bounded native operands')
        need(ins['target'] is None or type(ins['target']) is int and 0<=ins['target']<=1000000, 'Invalid native branch target')
    offsets=[x['offset'] for x in instructions]
    need(offsets==sorted(set(offsets)) and offsets[0]==0,'Native offsets must be unique, ordered, and start at zero')
    need(all(type(x['size']) is int and 1<=x['size']<=15 for x in instructions),'Invalid x64 instruction byte length')
    need(all(a['offset']+a['size']==b['offset'] for a,b in zip(instructions,instructions[1:])), 'Native instruction stream has an unparsed gap or overlap')
    byoffset={x['offset']:i for i,x in enumerate(instructions)}
    need(not any(re.match(r'v?f(?:n?madd|n?msub|maddsub|msubadd)',x['mnemonic']) for x in instructions),'FMA/native contraction found')
    summary={};all_packed=[]
    for scenario in SCENARIOS:
        regs={'rax':('C',),'rdx':('BP',),'rcx':('EP',)}
        queue=[(0,regs,{},None,[])];seen=set();outcomes=[];steps=0
        while queue:
            pos,regs,spill,flags,path=queue.pop();steps+=1
            need(steps<=MAX_STATES,'Unsupported native CFG state growth')
            if pos>=len(instructions):raise NativeProofError('Native CFG falls off function')
            state=(pos,repr(sorted(regs.items())),repr(sorted(spill.items())),repr(flags))
            if state in seen:continue
            seen.add(state);ins=instructions[pos];op=ins['mnemonic'];args=ins['operands'];path=path+[ins['offset']]
            need(len(path)<=MAX_INSTRUCTIONS*2,'Unresolved native CFG loop before arithmetic')
            vals=lambda: [operand_value(a,regs,spill) for a in args]
            if op.startswith('ret'):
                outcomes.append(('return',ins['offset'],[]));continue
            if op in ('ud2','int3'):continue # Valid in-bounds callback metadata excludes traps.
            if op in ('jmp','jmpq') or op.startswith('j'):
                need(ins['target'] in byoffset,'Indirect/out-of-function native branch unsupported')
                target=byoffset[ins['target']]
                choice=True if op in ('jmp','jmpq') else condition(op[1:],flags[0],flags[1],scenario) if flags else None
                if choice is not False:queue.append((target,copy.deepcopy(regs),copy.deepcopy(spill),flags,path))
                if choice is not True:queue.append((pos+1,copy.deepcopy(regs),copy.deepcopy(spill),flags,path))
                continue
            if op.startswith('set') and len(args)==1:
                regs[gp(args[0])]=('bool',op[3:],flags[0],flags[1]) if flags else ('unknown_set',);queue.append((pos+1,regs,spill,flags,path));continue
            if op.startswith('cmp') and not op.startswith('cmpps') and len(args)==2:
                flags=tuple(vals())
            elif op.startswith('test') and len(args)==2:
                a,b=vals();flags=(a if a==b else ('and',a,b),0)
            elif op in ('movl','movq','movw','movb','movzxbl','movzxwl','movsxlq','movzxbq','movzxwq') and len(args)==2:
                value=vals()[1]
                if '[' in args[0]:spill[args[0]]=value
                else:regs[gp(args[0])]=value
            elif op in ('leal','leaq') and len(args)==2:
                regs[gp(args[0])]=memory_address(args[1],regs) or ('unknown_lea',)
            elif op in ('addl','addq','subl','subq','andl','andq','orl','orq','xorl','xorq','imull','imulq','shll','shlq','sall','salq') and len(args)>=2:
                a,b=vals()[-2:];dest=gp(args[0])
                if op.startswith('add'):v=add(a,b)
                elif op.startswith('sub'):v=sub(a,b)
                elif op.startswith('and'):v=('and',a,b)
                elif op.startswith('or'):v=('or',a,b)
                elif op.startswith('xor'):v=0 if a==b else ('unknown_xor',)
                elif op.startswith(('shl','sal')):v=('mul',a,1<<b) if isinstance(b,int) else ('unknown_shift',)
                else:v=('mul',a,b)
                regs[dest]=v;flags=(v,0)
            elif op in ('vxorps','vpxor','xorps','pxor') and len(args)>=2:
                regs[gp(args[0])]=0 if args[-1]==args[-2] else ('unknown_vector',op)
            elif op in ('vbroadcastss','vpbroadcastd','vshufps','vpshufd','shufps','pshufd'):
                values=vals()[1:];nonimm=[v for v in values if not isinstance(v,int)]
                regs[gp(args[0])]=('A',) if nonimm and all(v==('A',) for v in nonimm) else ('unknown_splat',)
            elif op in ('vmovups','vmovaps','vmovdqu','vmovdqa','movups','movaps','movdqu','movdqa','vmovss','movss','vmovd','movd') and len(args)>=2:
                value=vals()[-1]
                if '[' in args[0]:
                    if 'rbp' in args[0] or 'rsp' in args[0]:spill[args[0]]=value
                    elif op.endswith('ss'):
                        valid_x = isinstance(value,tuple) and value[0]=='X'
                        valid_product = isinstance(value,tuple) and value[0]=='scalar_product'
                        need(valid_x or valid_product, 'Scalar store is disconnected from input/alpha dataflow')
                        address=memory_address(args[0],regs)
                        need(has(address,'O') or (scenario.startswith('exact') and has(address,'I')), 'Scalar store is not connected to output pointer')
                        # A token scalar instruction/store before a vector path is not
                        # evidence of a scalar-only fallback. Check every downstream
                        # CFG edge conservatively, including loop backedges.
                        pending=[pos+1];visited_after=set()
                        while pending:
                            at=pending.pop()
                            if at in visited_after or at>=len(instructions):continue
                            visited_after.add(at);later=instructions[at];lm=later['mnemonic'];la=later['operands']
                            if lm in ('vmovups','vmovaps','vmovdqu','vmovdqa','movups','movaps','movdqu','movdqa') and la and '[' in la[0] and not ('rbp' in la[0] or 'rsp' in la[0]):
                                raise NativeProofError('Packed store reachable after claimed scalar fallback')
                            if lm.startswith('ret') or lm in ('ud2','int3'):continue
                            if lm.startswith('j'):
                                need(later['target'] in byoffset,'Unresolved scalar-fallback successor')
                                pending.append(byoffset[later['target']])
                                if lm in ('jmp','jmpq'):continue
                            pending.append(at+1)
                        outcomes.append(('scalar',ins['offset'],[]));continue
                    else:
                        need(isinstance(value,tuple) and value[0]=='selected','Packed store lacks connected strict mul/ordered-compare/bitselect dataflow')
                        address=memory_address(args[0],regs)
                        need(has(address,'O') or (scenario.startswith('exact') and has(address,'I')),'Packed store is not connected to output pointer')
                        width=256 if any(a.startswith('ymm') for a in args) else 128
                        outcomes.append(('packed_strict',ins['offset'],path));all_packed.append({'scenario':scenario,'store_offset':ins['offset'],'width_bits':width});continue
                else:regs[gp(args[0])]=value
            elif op in ('vmulps','mulps'):
                values=vals();inputs=values[-2:] if len(values)>=3 else values
                regs[gp(args[0])]=vector_select('mul',inputs)
            elif op in ('vmulss','mulss'):
                values=vals();inputs=values[-2:] if len(values)>=3 else values
                product=vector_select('mul',inputs)
                regs[gp(args[0])]=('scalar_product',product[1]) if product[0]=='product' else ('unknown_scalar_product',)
            elif op in ('vcmpps','cmpps','vcmpleps','vcmpltps','vcmpgeps'):
                values=vals();predicate=None
                if op.endswith('leps'):predicate=2;pair=values[-2:]
                elif op.endswith('ltps'):predicate=1;pair=values[-2:]
                elif op.endswith('geps'):predicate=13;pair=values[-2:]
                else:
                    need(len(values)>=3 and isinstance(values[-1],int),'Unsupported packed compare encoding')
                    predicate=values[-1];pair=values[-3:-1] if len(values)==4 else values[:-1]
                a,b=pair
                x=b if predicate==2 and a==0 else a if predicate==13 and b==0 else None
                regs[gp(args[0])]=('ge0',x) if isinstance(x,tuple) and x[0]=='X' else ('wrong_compare',predicate)
            elif op in ('vpandn','vandnps','pandn','andnps','vpand','vandps','pand','andps','vpor','vorps','por','orps'):
                values=vals();inputs=values[-2:] if len(values)>=3 else values
                operation='andnot' if 'andn' in op else 'and' if 'and' in op else 'or'
                regs[gp(args[0])]=vector_select(operation,inputs)
            elif op=='vblendvps' and len(args)==4:
                regs[gp(args[0])]=vector_select('blend',vals()[1:])
            elif op in ('call','callq'):
                # No undocumented preserving-call assumption. Destroy every
                # tracked non-frame register and all vector facts. A legitimate
                # stack-check path must restore required facts from saved slots.
                for name in list(regs):
                    if name not in ('rbp','rsp'):regs[name]=('unknown_call_clobber',name)
                flags=None
            elif op in ('nop','nopl','nopw','push','pushq','vzeroupper','ucomiss','vucomiss'):
                if 'ucomiss' in op:flags=None
            elif op.startswith('pop') and args:
                regs[gp(args[0])]=('unknown_pop',)
            else:
                raise NativeProofError('Unsupported native instruction: '+op)
            queue.append((pos+1,regs,spill,flags,path))
        kinds=sorted({x[0] for x in outcomes})
        expected='packed_strict' if scenario=='disjoint_ge4' or scenario=='exact_ge4' and variant=='candidate' else 'scalar'
        if scenario=='exact_short':need(bool(kinds) and set(kinds)<={'scalar','return'},'Short-input path can reach packed operations or is unproved')
        else:need(kinds==[expected],f'Native alias/length CFG proof failed: {scenario} -> {kinds}, expected {expected}')
        summary[scenario]={'first_arithmetic_paths':kinds,'terminal_offsets':sorted({x[1] for x in outcomes})}
    need(all_packed,'No connected packed operation path')
    return {'scenario_proof':summary,'packed_stores':sorted({(x['scenario'],x['store_offset'],x['width_bits']) for x in all_packed}),
            'no_fma':True,'pointer_metadata_offsets':[8,12],'alpha_metadata_offset':16,
            'scope':'Valid in-bounds metadata; x64 Wasm ABI parameters rax/rdx/rcx after implicit instance rsi. CFG and connected vector dataflow, not hardware branch tracing.'}


def parse_blocks(text, function_index):
    # Adapters should strip their own known logging prefixes. These two exact
    # Playwright debug prefixes are supported; arbitrary prefix stripping is not.
    normalized=[]
    for line in text.splitlines():
        line=re.sub(r'^\s*\S+\s+pw:browser\s+\[pid=\d+\]\[(?:out|err)\]\s?', '', line)
        line=re.sub(r'^\s*\[pid=\d+\]\[(?:out|err)\]\s?', '', line)
        normalized.append(line)
    text='\n'.join(normalized)
    blocks=re.findall(r'--- WebAssembly code ---\s*(.*?)--- End code ---',text,re.S)
    need(blocks,'No complete V8 native code block')
    result=[]
    for block in blocks:
        match=re.search(r'^index:\s*(\d+)\s*$',block,re.M)
        name=re.search(r'^name:\s*wasm-function\[(\d+)\]\s*$',block,re.M)
        compiler=re.search(r'^compiler:\s*(\w+)\s*$',block,re.M)
        need(match and name and compiler and int(match[1])==int(name[1])==function_index,'Wrong/ambiguous native function binding')
        need(compiler[1] in ('Liftoff','TurboFan'),'Unsupported V8 native compiler')
        body=re.search(r'Instructions \(size\s*=\s*(\d+)\)\s*\n(.*?)(?=\n(?:Safepoints|RelocInfo|Source positions|Protected instructions|Inlined functions|Deoptimization)|\Z)',block,re.S)
        need(body,'Missing native instruction section')
        rows=[];base=None
        for line in body[2].splitlines():
            m=re.match(r'^\s*0x([0-9a-fA-F]+)\s+([0-9a-fA-F]+)\s+([0-9a-fA-F]+)\s+([a-z][a-z0-9]*)\s*(.*?)\s*$',line)
            if not m:
                need(not line.strip() or line.lstrip().startswith(';'), 'Unparsed line in native instruction stream')
                continue
            absolute,offset=int(m[1],16),int(m[2],16)
            encoding=m[3]
            need(len(encoding)%2==0 and 1<=len(encoding)//2<=15, 'Invalid printed x64 instruction encoding')
            size=len(encoding)//2
            if base is None:base=absolute-offset
            need(absolute-offset==base,'Native address/offset mismatch')
            op=m[4];rest=m[5].split(';',1)[0].strip();target=None
            if op.startswith('j'):
                dest=re.match(r'0x([0-9a-fA-F]+)',rest)
                need(dest,'Unsupported indirect native jump');target=int(dest[1],16)-base;rest=''
            elif op.startswith('call'):
                rest='<external>'
            rest=re.sub(r'\s*<[^>]*>','',rest)
            args=[x.strip() for x in rest.split(',')] if rest else []
            rows.append({'offset':offset,'size':size,'mnemonic':op,'operands':args,'target':target})
        need(rows and len(rows)<=MAX_INSTRUCTIONS,'Native block instructions unavailable/too large')
        need(rows[0]['offset']==0 and all(a['offset']+a['size']==b['offset'] for a,b in zip(rows,rows[1:])) and rows[-1]['offset']+rows[-1]['size']==int(body[1]), 'Native instruction byte coverage differs from claimed stream')
        # Printed nop/int3 padding is decoded normally. Body padding outside the
        # declared Instructions size is excluded; inline data or unprinted bytes
        # inside that stream are unsupported and cannot be silently skipped.
        result.append({'compiler':compiler[1],'instruction_bytes':int(body[1]),'instructions':rows})
    return result


def parse_native_dump(text, function_index, variant):
    need(type(function_index) is int and function_index>=0,'Invalid native function index')
    blocks=parse_blocks(text,function_index);optimized=[b for b in blocks if b['compiler']=='TurboFan']
    need(optimized,'Normal-tier trace lacks optimized callback; do not force tiering')
    evidence=[]
    for block in optimized:
        findings=analyze_instructions(block['instructions'],variant)
        evidence.append({'instruction_bytes':block['instruction_bytes'],'instructions':block['instructions'],'findings':findings})
    # Multiple instances are fine only when offset-normalized instruction proof agrees.
    fingerprints={digest({'instructions':x['instructions'],'findings':x['findings']}) for x in evidence}
    need(len(fingerprints)==1,'Multiple native instances disagree after normalization')
    proof={'schema':'inplace-leaky-native-cfg-v1','function_index':function_index,'variant':variant,
           'architecture':'x64','abi_source':ABI_SOURCE,'tiers_seen':sorted({b['compiler'] for b in blocks}),
           'optimized_blocks':len(optimized),'evidence':evidence[0],
           'instruction_evidence_sha256':digest(evidence[0]['instructions'])}
    validate_native_proof(proof,variant,function_index)
    return proof


def validate_native_proof(proof, variant, function_index):
    need(set(proof)=={'schema','function_index','variant','architecture','abi_source','tiers_seen','optimized_blocks','evidence','instruction_evidence_sha256'},'Unexpected native proof field')
    need(proof['schema']=='inplace-leaky-native-cfg-v1' and proof['variant']==variant and proof['function_index']==function_index and proof['architecture']=='x64' and proof['abi_source']==ABI_SOURCE,'Native proof identity mismatch')
    need(set(proof['tiers_seen'])<={'Liftoff','TurboFan'} and 'TurboFan' in proof['tiers_seen'] and type(proof['optimized_blocks']) is int and proof['optimized_blocks']>=1,'Native tier evidence missing')
    e=proof['evidence'];need(set(e)=={'instruction_bytes','instructions','findings'},'Native evidence schema')
    need(type(e['instruction_bytes']) is int and e['instruction_bytes']>0,'Native byte count')
    need(bool(e['instructions']) and e['instructions'][-1]['offset']+e['instructions'][-1]['size']==e['instruction_bytes'], 'Native instruction evidence does not cover declared bytes')
    need(proof['instruction_evidence_sha256']==digest(e['instructions']),'Native instruction evidence digest mismatch')
    calculated=analyze_instructions(e['instructions'],variant)
    # JSON round-tripping turns internal tuples into lists.
    need(json.loads(json.dumps(calculated))==json.loads(json.dumps(e['findings'])),'Native findings do not follow evidence')
    return True

FAILURE_CATEGORIES = frozenset({'native_dump_unavailable','function_binding','optimized_tier_not_observed','contraction_detected','conflicting_instances','cfg_or_dataflow','proof_integrity','unsupported_lowering'})


def native_failure_evidence(error):
    """Only fixed categories escape; exception text and machine paths never do."""
    message=str(error) if isinstance(error,NativeProofError) else ''
    if message.startswith('No complete V8'):category='native_dump_unavailable'
    elif message.startswith(('Wrong/ambiguous native function','Invalid native function','Native proof identity')):category='function_binding'
    elif message.startswith('Normal-tier trace lacks'):category='optimized_tier_not_observed'
    elif message.startswith('FMA/'):category='contraction_detected'
    elif message.startswith('Multiple native instances'):category='conflicting_instances'
    elif message.startswith(('Native alias/length','Packed store','Scalar store','Short-input')):category='cfg_or_dataflow'
    elif message.startswith(('Native instruction evidence','Native findings','Unexpected native proof','Native evidence schema')):category='proof_integrity'
    else:category='unsupported_lowering'
    result={'schema':'inplace-native-failure-v1','category':category,'error_type':'NativeProofError' if isinstance(error,NativeProofError) else 'OtherError'}
    validate_native_failure_evidence(result)
    return result


def validate_native_failure_evidence(value):
    need(set(value)=={'schema','category','error_type'} and value['schema']=='inplace-native-failure-v1' and value['category'] in FAILURE_CATEGORIES and value['error_type'] in ('NativeProofError','OtherError'),'Invalid bounded native failure evidence')
    return True
