import time

from pypy.tool.isolate import Isolate 

from pypy.translator.llvm import buildllvm
from pypy.translator.llvm.database import Database 
from pypy.rpython.rmodel import inputconst
from pypy.rpython.typesystem import getfunctionptr
from pypy.rpython.lltypesystem import lltype
from pypy.tool.udir import udir
from pypy.translator.llvm.codewriter import CodeWriter
from pypy.translator.llvm import extfuncnode
from pypy.translator.llvm.module.support import extfunctions
from pypy.translator.llvm.node import Node
from pypy.translator.llvm.externs2ll import setup_externs, generate_llfile
from pypy.translator.llvm.gc import GcPolicy
from pypy.translator.llvm.log import log
from pypy.rlib.nonconst import NonConstant
from pypy.annotation.listdef import s_list_of_strings
from pypy.rpython.annlowlevel import MixLevelHelperAnnotator
from pypy.annotation import model as annmodel
from pypy.rpython.lltypesystem import rffi

def augment_entrypoint(translator, entrypoint):
    bk = translator.annotator.bookkeeper
    graph_entrypoint = bk.getdesc(entrypoint).getuniquegraph()
    s_result = translator.annotator.binding(graph_entrypoint.getreturnvar())

    get_argc = rffi.llexternal('_pypy_getargc', [], rffi.INT)
    get_argv = rffi.llexternal('_pypy_getargv', [], rffi.CCHARPP)

    def new_entrypoint():
        argc = get_argc()
        argv = get_argv()
        args = [rffi.charp2str(argv[i]) for i in range(argc)]
        return entrypoint(args)

    entrypoint._annenforceargs_ = [s_list_of_strings]
    mixlevelannotator = MixLevelHelperAnnotator(translator.rtyper)

    graph = mixlevelannotator.getgraph(new_entrypoint, [], s_result)
    mixlevelannotator.finish()
    mixlevelannotator.backend_optimize()
    
    return new_entrypoint

class GenLLVM(object):
    debug = False
    
    # see create_codewriter() below
    function_count = {}

    def __init__(self, translator, standalone):
    
        # reset counters
        Node.nodename_count = {}    

        self.standalone = standalone
        self.translator = translator
        
        self.config = translator.config

    def gen_source(self, func):
        self._checkpoint("before gen source")

        codewriter = self.setup(func)

        codewriter.header_comment("Extern code")
        codewriter.write_lines(self.llcode)

        codewriter.header_comment("Type declarations")
        for typ_decl in self.db.gettypedefnodes():
            typ_decl.writetypedef(codewriter)

        codewriter.header_comment("Function prototypes")
        for node in self.db.getnodes():
            if hasattr(node, 'writedecl'):
                node.writedecl(codewriter)

        codewriter.header_comment("Prebuilt constants")
        for node in self.db.getnodes():
            # XXX tmp
            if hasattr(node, "writeglobalconstants"):
                node.writeglobalconstants(codewriter)

        self._checkpoint("before definitions")

        codewriter.header_comment('Suppport definitions')
        codewriter.write_lines(extfunctions, patch=True)

        codewriter.header_comment('Startup definition')
        self.write_startup_impl(codewriter)

        codewriter.header_comment("Function definitions")
        for node in self.db.getnodes():
            if hasattr(node, 'writeimpl'):
                node.writeimpl(codewriter)

        self._debug()
        
        codewriter.comment("End of file")
        codewriter.close()
        self._checkpoint('done')

        return self.filename

    def setup(self, func):
        """ setup all nodes
            create c file for externs
            create ll file for c file
            create codewriter """

        if self.standalone:
            func = augment_entrypoint(self.translator, func)

        # XXX please dont ask!
        from pypy.translator.c.genc import CStandaloneBuilder
        cbuild = CStandaloneBuilder(self.translator, func, config=self.config)
        #cbuild.stackless = self.stackless
        c_db = cbuild.generate_graphs_for_llinterp()

        self.db = Database(self, self.translator)
        self.db.gcpolicy = GcPolicy.new(self.db, self.config.translation.gc)

        # get entry point
        entry_point = self.get_entry_point(func)
        self._checkpoint('get_entry_point')
        
        # set up all nodes
        self.db.setup_all()
        
        self.entrynode = self.db.set_entrynode(entry_point)
        self._checkpoint('setup_all all nodes')

        # set up externs nodes
        self.extern_decls = setup_externs(c_db, self.db)
        self.translator.rtyper.specialize_more_blocks()
        self.db.setup_all()
        self._checkpoint('setup_all externs')

        for node in self.db.getnodes():
            node.post_setup_transform()
        
        self._print_node_stats()

        # open file & create codewriter
        codewriter, self.filename = self.create_codewriter()
        self._checkpoint('open file and create codewriter')        

        # create ll file from c code
        self.generate_ll_externs(codewriter)
        self._checkpoint('setup_externs')

        return codewriter
   
    def get_entry_point(self, func):
        assert func is not None
        self.entrypoint = func

        bk = self.translator.annotator.bookkeeper
        ptr = getfunctionptr(bk.getdesc(func).getuniquegraph())
        c = inputconst(lltype.typeOf(ptr), ptr)
        self.db.prepare_arg_value(c)
        
        # ensure unqiue entry node name for testing
        entry_node = self.db.obj2node[c.value._obj]
        name = entry_node.name
        if name in self.function_count:
            self.function_count[name] += 1
            Node.nodename_count[name] = self.function_count[name] + 1
            name += '_%d' % self.function_count[name]
            entry_node.name =  name
        else:
            self.function_count[name] = 1

        self.entry_name = name[6:]
        return c.value._obj 

    def generate_ll_externs(self, codewriter):
        c_includes = {}
        c_sources = {}

        for node in self.db.getnodes():
            includes, sources = node.external_c_source()            
            for include in includes:
                c_includes[include] = True
            for source in sources:
                c_sources[source] = True

        self.llcode = generate_llfile(self.db,
                                      self.extern_decls,
                                      self.entrynode,
                                      c_includes,
                                      c_sources,
                                      self.standalone, 
                                      codewriter.cconv)

    def create_codewriter(self):
        # prevent running the same function twice in a test
        filename = udir.join(self.entry_name).new(ext='.ll')
        f = open(str(filename), 'w')
        if self.standalone:
            return CodeWriter(f, self.db), filename
        else:
            return CodeWriter(f, self.db, cconv='ccc', linkage=''), filename
                
    def write_startup_impl(self, codewriter):
        open_decl =  "i8* @LLVM_RPython_StartupCode()"
        codewriter.openfunc(open_decl)
        for node in self.db.getnodes():
            node.writesetupcode(codewriter)

        codewriter.ret("i8*", "null")
        codewriter.closefunc()

    def compile_module(self):
        assert not self.standalone
        
        modname, dirpath = buildllvm.Builder(self).make_module()
        mod, wrap_fun = self.get_module(modname, dirpath)
        return mod, wrap_fun

    def get_module(self, modname, dirpath):
        if self.config.translation.llvm.isolate:
            mod = Isolate((dirpath, modname))
        else:
            from pypy.translator.tool.cbuild import import_module_from_directory
            mod = import_module_from_directory(dirpath, modname)

        wrap_fun = getattr(mod, 'entrypoint')
        return mod, wrap_fun

    def compile_standalone(self, exe_name):
        assert self.standalone
        return buildllvm.Builder(self).make_standalone(exe_name)

    def _checkpoint(self, msg=None):
        if not self.config.translation.llvm.logging:
            return
        if msg:
            t = (time.time() - self.starttime)
            log('\t%s took %02dm%02ds' % (msg, t/60, t%60))
        else:
            log('GenLLVM:')
        self.starttime = time.time()

    def _print_node_stats(self):
        # disable node stats output
        if not self.config.translation.llvm.logging: 
            return 

        nodecount = {}
        for node in self.db.getnodes():
            typ = type(node)
            try:
                nodecount[typ] += 1
            except:
                nodecount[typ] = 1
        stats = [(count, str(typ)) for typ, count in nodecount.iteritems()]
        stats.sort()
        for s in stats:
            log('STATS %s' % str(s))

    def _debug(self):
        if self.debug:
            if self.db.debugstringnodes:            
                codewriter.header_comment("Debug string")
                for node in self.db.debugstringnodes:
                    node.writeglobalconstants(codewriter)

            #print "Start"
            #print self.db.dump_pbcs()
            #print "End"
