import sys

import pytest

from pypy.tool.cpyext.extbuild import SystemCompilationInfo, HERE
from pypy.interpreter.gateway import unwrap_spec, interp2app, ObjSpace
from pypy.interpreter.error import OperationError
from rpython.rtyper.lltypesystem import lltype
from pypy.module.cpyext import api
from pypy.module.cpyext.api import cts, create_extension_module
from pypy.module.cpyext.pyobject import from_ref
from pypy.module.cpyext.state import State
from rpython.tool import leakfinder
from rpython.rlib import rawrefcount
from rpython.tool.udir import udir

import pypy.module.cpyext.moduledef  # Make sure all the functions are registered

only_pypy ="config.option.runappdirect and '__pypy__' not in sys.builtin_module_names"

@api.cpython_api([], api.PyObject)
def PyPy_Crash1(space):
    1/0

@api.cpython_api([], lltype.Signed, error=-1)
def PyPy_Crash2(space):
    1/0

@api.cpython_api([api.PyObject], api.PyObject, result_is_ll=True)
def PyPy_Noop(space, pyobj):
    return pyobj

class TestApi:
    def test_signature(self):
        common_functions = api.FUNCTIONS_BY_HEADER[api.pypy_decl]
        assert 'PyModule_Check' in common_functions
        assert common_functions['PyModule_Check'].argtypes == [cts.gettype("void *")]
        assert 'PyModule_GetDict' in common_functions
        assert common_functions['PyModule_GetDict'].argtypes == [api.PyObject]


class SpaceCompiler(SystemCompilationInfo):
    """Extension compiler for regular (untranslated PyPy) mode"""
    def __init__(self, space, *args, **kwargs):
        self.space = space
        SystemCompilationInfo.__init__(self, *args, **kwargs)

    def load_module(self, mod, name, use_imp=False):
        space = self.space
        w_path = space.newtext(mod)
        w_name = space.newtext(name)
        if use_imp:
            # this is VERY slow and should be used only by tests which
            # actually needs it
            return space.appexec([w_name, w_path], '''(name, path):
                import imp
                return imp.load_dynamic(name, path)''')
        else:
            w_spec = space.appexec([w_name, w_path], '''(modname, path):
                class FakeSpec:
                    name = modname
                    origin = path
                return FakeSpec
            ''')
            w_mod = create_extension_module(space, w_spec)
            return w_mod


def get_cpyext_info(space):
    from pypy.module.imp.importing import get_so_extension
    state = space.fromcache(State)
    api_library = state.api_lib
    if sys.platform == 'win32':
        libraries = [api_library]
        # '%s' undefined; assuming extern returning int
        compile_extra = ["/we4013"]
        # prevent linking with PythonXX.lib
        w_maj, w_min = space.fixedview(space.sys.get('version_info'), 5)[:2]
        link_extra = ["/NODEFAULTLIB:Python%d%d.lib" %
            (space.int_w(w_maj), space.int_w(w_min))]
    else:
        libraries = []
        if sys.platform.startswith('linux'):
            compile_extra = [
                "-Werror", "-g", "-O0", "-Wp,-U_FORTIFY_SOURCE", "-fPIC"]
            link_extra = ["-g"]
        else:
            compile_extra = link_extra = None
    return SpaceCompiler(space,
        builddir_base=udir,
        include_extra=api.include_dirs,
        compile_extra=compile_extra,
        link_extra=link_extra,
        extra_libs=libraries,
        ext=get_so_extension(space))


def freeze_refcnts(self):
    rawrefcount._dont_free_any_more()

def is_interned_string(space, w_obj):
    try:
        u = space.utf8_w(w_obj)
    except OperationError:
        return False
    return space.interned_strings.get(u) is not None

def is_allowed_to_leak(space, obj):
    from pypy.module.cpyext.methodobject import W_PyCFunctionObject
    try:
        w_obj = from_ref(space, cts.cast('PyObject*', obj._as_ptr()))
    except:
        return False
    if isinstance(w_obj, W_PyCFunctionObject):
        return True
    # It's OK to "leak" some interned strings: if the pyobj is created by
    # the test, but the w_obj is referred to from elsewhere.
    return is_interned_string(space, w_obj)

def _get_w_obj(space, c_obj):
    return from_ref(space, cts.cast('PyObject*', c_obj._as_ptr()))

class CpyextLeak(leakfinder.MallocMismatch):
    def __str__(self):
        lines = [leakfinder.MallocMismatch.__str__(self), '']
        lines.append(
            "These objects are attached to the following W_Root objects:")
        for c_obj in self.args[0]:
            try:
                lines.append("  %s" % (_get_w_obj(self.args[1], c_obj),))
            except:
                pass
        return '\n'.join(lines)


class LeakCheckingTest(object):
    """Base class for all cpyext tests."""
    spaceconfig = {"usemodules" : ['cpyext', 'thread', 'struct', 'array',
                                   'itertools', 'time', 'binascii',
                                   'mmap', 'signal',
                                   '_cffi_backend',
                                   ],
                   "objspace.disable_entrypoints_in_cffi": True}
    spaceconfig["objspace.std.withspecialisedtuple"] = True

    @classmethod
    def preload_builtins(cls, space):
        """
        Eagerly create pyobjs for various builtins so they don't look like
        leaks.
        """
        from pypy.module.cpyext.pyobject import make_ref
        w_to_preload = space.appexec([], """():
            import sys
            import mmap
            #
            # copied&pasted to avoid importing the whole types.py, which is
            # expensive on py3k
            # <types.py>
            def _f(): pass
            FunctionType = type(_f)
            CodeType = type(_f.__code__)
            try:
                raise TypeError
            except TypeError:
                tb = sys.exc_info()[2]
                TracebackType = type(tb)
                FrameType = type(tb.tb_frame)
                del tb
            # </types.py>
            return [
                #buffer,   ## does not exist on py3k
                mmap.mmap,
                FunctionType,
                CodeType,
                TracebackType,
                FrameType,
                type(str.join),
            ]
        """)
        for w_obj in space.unpackiterable(w_to_preload):
            make_ref(space, w_obj)

    def cleanup(self):
        self.space.getexecutioncontext().cleanup_cpyext_state()
        for _ in range(5):
            rawrefcount._collect()
            self.space.user_del_action._run_finalizers()
        try:
            # set check=True to actually enable leakfinder
            leakfinder.stop_tracking_allocations(check=False)
        except leakfinder.MallocMismatch as e:
            result = e.args[0]
            filtered_result = {}
            for obj, value in result.iteritems():
                if not is_allowed_to_leak(self.space, obj):
                    filtered_result[obj] = value
            if filtered_result:
                raise CpyextLeak(filtered_result, self.space)
        assert not self.space.finalizer_queue.next_dead()


class AppTestApi(LeakCheckingTest):
    def setup_class(cls):
        from rpython.rlib.clibffi import get_libc_name
        if cls.runappdirect:
            cls.libc = get_libc_name()
        else:
            cls.w_libc = cls.space.wrap(get_libc_name())

    def setup_method(self, meth):
        if not self.runappdirect:
            freeze_refcnts(self)

    def teardown_method(self, meth):
        if self.runappdirect:
            return
        self.cleanup()

    @pytest.mark.skipif(only_pypy, reason='pypy only test')
    def test_only_import(self):
        import cpyext

    def test_dllhandle(self):
        import sys
        if sys.platform != "win32" or sys.version_info < (2, 6):
            skip("Windows Python >= 2.6 only")
        assert isinstance(sys.dllhandle, int)


def _unwrap_include_dirs(space, w_include_dirs):
    if w_include_dirs is None:
        return None
    else:
        return [space.text_w(s) for s in space.listview(w_include_dirs)]

def debug_collect(space):
    rawrefcount._collect()

def in_pygclist(space, int_addr):
    return space.wrap(rawrefcount._in_pygclist(int_addr))

class AppTestCpythonExtensionBase(LeakCheckingTest):
    def setup_class(cls):
        space = cls.space
        cls.w_here = space.wrap(str(HERE))
        cls.w_udir = space.wrap(str(udir))
        cls.w_runappdirect = space.wrap(cls.runappdirect)
        if not cls.runappdirect:
            cls.sys_info = get_cpyext_info(space)
            cls.w_debug_collect = space.wrap(interp2app(debug_collect))
            cls.w_in_pygclist = space.wrap(
                interp2app(in_pygclist, unwrap_spec=[ObjSpace, int]))
            cls.preload_builtins(space)
        else:
            def w_import_module(self, name, init=None, body='', filename=None,
                    include_dirs=None, PY_SSIZE_T_CLEAN=False, use_imp=False):
                from extbuild import get_sys_info_app
                sys_info = get_sys_info_app(self.udir)
                return sys_info.import_module(
                    name, init=init, body=body, filename=filename,
                    include_dirs=include_dirs,
                    PY_SSIZE_T_CLEAN=PY_SSIZE_T_CLEAN)
            cls.w_import_module = w_import_module

            def w_import_extension(self, modname, functions, prologue="",
                include_dirs=None, more_init="", PY_SSIZE_T_CLEAN=False):
                from extbuild import get_sys_info_app
                sys_info = get_sys_info_app(self.udir)
                return sys_info.import_extension(
                    modname, functions, prologue=prologue,
                    include_dirs=include_dirs, more_init=more_init,
                    PY_SSIZE_T_CLEAN=PY_SSIZE_T_CLEAN)
            cls.w_import_extension = w_import_extension

            def w_compile_module(self, name,
                    source_files=None, source_strings=None):
                from extbuild import get_sys_info_app
                sys_info = get_sys_info_app(self.udir)
                return sys_info.compile_extension_module(name,
                    source_files=source_files, source_strings=source_strings)
            cls.w_compile_module = w_compile_module

            def w_load_module(self, mod, name):
                from extbuild import get_sys_info_app
                sys_info = get_sys_info_app(self.udir)
                return sys_info.load_module(mod, name)
            cls.w_load_module = w_load_module

            def w_debug_collect(self):
                import gc
                gc.collect()
                gc.collect()
                gc.collect()
            cls.w_debug_collect = w_debug_collect


    def record_imported_module(self, name):
        """
        Record a module imported in a test so that it can be cleaned up in
        teardown before the check for leaks is done.

        name gives the name of the module in the space's sys.modules.
        """
        self.imported_module_names.append(name)

    def setup_method(self, func):
        if self.runappdirect:
            return

        @unwrap_spec(name='text')
        def compile_module(space, name,
                           w_source_files=None,
                           w_source_strings=None):
            """
            Build an extension module linked against the cpyext api library.
            """
            if not space.is_none(w_source_files):
                source_files = space.unwrap(w_source_files)
            else:
                source_files = None
            if not space.is_none(w_source_strings):
                source_strings = space.listview_bytes(w_source_strings)
            else:
                source_strings = None
            pydname = self.sys_info.compile_extension_module(
                name,
                source_files=source_files,
                source_strings=source_strings)

            # hackish, but tests calling compile_module() always end up
            # importing the result
            self.record_imported_module(name)

            return space.wrap(pydname)

        @unwrap_spec(name='text', init='text_or_none', body='text',
                     filename='fsencode_or_none', PY_SSIZE_T_CLEAN=bool,
                     use_imp=bool)
        def import_module(space, name, init=None, body='',
                          filename=None, w_include_dirs=None,
                          PY_SSIZE_T_CLEAN=False, use_imp=False):
            include_dirs = _unwrap_include_dirs(space, w_include_dirs)
            w_result = self.sys_info.import_module(
                name, init, body, filename, include_dirs, PY_SSIZE_T_CLEAN,
                use_imp)
            self.record_imported_module(name)
            return w_result

        @unwrap_spec(mod='text', name='text')
        def load_module(space, mod, name):
            return self.sys_info.load_module(mod, name)

        @unwrap_spec(modname='text', prologue='text',
                             more_init='text', PY_SSIZE_T_CLEAN=bool)
        def import_extension(space, modname, w_functions, prologue="",
                             w_include_dirs=None, more_init="", PY_SSIZE_T_CLEAN=False):
            functions = space.unwrap(w_functions)
            include_dirs = _unwrap_include_dirs(space, w_include_dirs)
            w_result = self.sys_info.import_extension(
                modname, functions, prologue, include_dirs, more_init,
                PY_SSIZE_T_CLEAN)
            self.record_imported_module(modname)
            return w_result

        # A list of modules which the test caused to be imported (in
        # self.space).  These will be cleaned up automatically in teardown.
        self.imported_module_names = []

        wrap = self.space.wrap
        self.w_compile_module = wrap(interp2app(compile_module))
        self.w_load_module = wrap(interp2app(load_module))
        self.w_import_module = wrap(interp2app(import_module))
        self.w_import_extension = wrap(interp2app(import_extension))

        # create the file lock before we count allocations
        self.space.call_method(self.space.sys.get("stdout"), "flush")

        freeze_refcnts(self)

    def unimport_module(self, name):
        """
        Remove the named module from the space's sys.modules.
        """
        w_modules = self.space.sys.get('modules')
        w_name = self.space.wrap(name)
        self.space.delitem(w_modules, w_name)

    def teardown_method(self, func):
        if self.runappdirect:
            self.w_debug_collect()
            return
        debug_collect(self.space)
        for name in self.imported_module_names:
            self.unimport_module(name)
        self.cleanup()
        state = self.space.fromcache(State)
        assert 'operror' not in dir(state)


class AppTestCpythonExtension(AppTestCpythonExtensionBase):
    def test_createmodule(self):
        import sys
        self.import_module(name='foo')
        assert 'foo' in sys.modules

    def test_export_function(self):
        import sys
        if '__pypy__' in sys.modules:
            from cpyext import is_cpyext_function
        else:
            import inspect
            is_cpyext_function = inspect.isbuiltin
        body = """
        PyObject* foo_pi(PyObject* self, PyObject *args)
        {
            return PyFloat_FromDouble(3.14);
        }
        static PyMethodDef methods[] = {
            { "return_pi", foo_pi, METH_NOARGS },
            { NULL }
        };
        static struct PyModuleDef moduledef = {
            PyModuleDef_HEAD_INIT,
            "foo",          /* m_name */
            NULL,           /* m_doc */
            -1,             /* m_size */
            methods,        /* m_methods */
        };
        """
        module = self.import_module(name='foo', body=body)
        assert 'foo' in sys.modules
        assert 'return_pi' in dir(module)
        assert module.return_pi is not None
        assert is_cpyext_function(module.return_pi)
        assert module.return_pi() == 3.14
        assert module.return_pi.__module__ == 'foo'


    def test_export_docstring(self):
        body = """
        PyDoc_STRVAR(foo_pi_doc, "Return pi.");
        PyObject* foo_pi(PyObject* self, PyObject *args)
        {
            return PyFloat_FromDouble(3.14);
        }
        static PyMethodDef methods[] = {
            { "return_pi", foo_pi, METH_NOARGS, foo_pi_doc },
            { NULL }
        };
        static struct PyModuleDef moduledef = {
            PyModuleDef_HEAD_INIT,
            "foo",          /* m_name */
            NULL,           /* m_doc */
            -1,             /* m_size */
            methods,        /* m_methods */
        };
        """
        module = self.import_module(name='foo', body=body)
        doc = module.return_pi.__doc__
        assert doc == "Return pi."

    def test_load_dynamic(self):
        import sys
        body = """
        static struct PyModuleDef moduledef = {
            PyModuleDef_HEAD_INIT,
            "%(modname)s",  /* m_name */
            NULL,           /* m_doc */
            -1,             /* m_size */
            NULL,           /* m_methods */
        };
        """
        foo = self.import_module(name='foo', body=body, use_imp=True)
        assert 'foo' in sys.modules
        del sys.modules['foo']
        import imp
        foo2 = imp.load_dynamic('foo', foo.__file__)
        assert 'foo' in sys.modules
        assert foo.__dict__ == foo2.__dict__

    def test_InitModule4_dotted(self):
        """
        If the module name passed to Py_InitModule4 includes a package, only
        the module name (the part after the last dot) is considered when
        computing the name of the module initializer function.
        """
        expected_name = "pypy.module.cpyext.test.dotted"
        module = self.import_module(name=expected_name, filename="dotted")
        assert module.__name__ == expected_name


    def test_InitModule4_in_package(self):
        """
        If `apple.banana` is an extension module which calls Py_InitModule4 with
        only "banana" as a name, the resulting module nevertheless is stored at
        `sys.modules["apple.banana"]`.
        """
        module = self.import_module(name="apple.banana", filename="banana")
        assert module.__name__ == "apple.banana"


    def test_recursive_package_import(self):
        """
        If `cherry.date` is an extension module which imports `apple.banana`,
        the latter is added to `sys.modules` for the `"apple.banana"` key.
        """
        import sys, types, os
        # Build the extensions.
        banana = self.compile_module(
            "apple.banana", source_files=[os.path.join(self.here, 'banana.c')])
        date = self.compile_module(
            "cherry.date", source_files=[os.path.join(self.here, 'date.c')])

        # Set up some package state so that the extensions can actually be
        # imported.
        cherry = sys.modules['cherry'] = types.ModuleType('cherry')
        cherry.__path__ = [os.path.dirname(date)]

        apple = sys.modules['apple'] = types.ModuleType('apple')
        apple.__path__ = [os.path.dirname(banana)]

        import cherry.date

        assert sys.modules['apple.banana'].__name__ == 'apple.banana'
        assert sys.modules['cherry.date'].__name__ == 'cherry.date'


    def test_modinit_func(self):
        """
        A module can use the PyMODINIT_FUNC macro to declare or define its
        module initializer function.
        """
        module = self.import_module(name='modinit')
        assert module.__name__ == 'modinit'


    def test_export_function2(self):
        body = """
        static PyObject* my_objects[1];
        static PyObject* foo_cached_pi(PyObject* self, PyObject *args)
        {
            if (my_objects[0] == NULL) {
                my_objects[0] = PyFloat_FromDouble(3.14);
            }
            Py_INCREF(my_objects[0]);
            return my_objects[0];
        }
        static PyObject* foo_drop_pi(PyObject* self, PyObject *args)
        {
            if (my_objects[0] != NULL) {
                Py_DECREF(my_objects[0]);
                my_objects[0] = NULL;
            }
            Py_INCREF(Py_None);
            return Py_None;
        }
        static PyObject* foo_retinvalid(PyObject* self, PyObject *args)
        {
            return (PyObject*)0xAFFEBABE;
        }
        static PyMethodDef methods[] = {
            { "return_pi", foo_cached_pi, METH_NOARGS },
            { "drop_pi",   foo_drop_pi, METH_NOARGS },
            { "return_invalid_pointer", foo_retinvalid, METH_NOARGS },
            { NULL }
        };
        static struct PyModuleDef moduledef = {
            PyModuleDef_HEAD_INIT,
            "%(modname)s",  /* m_name */
            NULL,           /* m_doc */
            -1,             /* m_size */
            methods,        /* m_methods */
        };
        """
        module = self.import_module(name='foo', body=body)
        assert module.return_pi() == 3.14
        module.drop_pi()
        module.drop_pi()
        assert module.return_pi() == 3.14
        assert module.return_pi() == 3.14
        module.drop_pi()
        skip("Hmm, how to check for the exception?")
        raises(api.InvalidPointerException, module.return_invalid_pointer)

    def test_argument(self):
        import sys
        body = """
        PyObject* foo_test(PyObject* self, PyObject *args)
        {
            PyObject *t = PyTuple_GetItem(args, 0);
            Py_INCREF(t);
            return t;
        }
        static PyMethodDef methods[] = {
            { "test", foo_test, METH_VARARGS },
            { NULL }
        };
        static struct PyModuleDef moduledef = {
            PyModuleDef_HEAD_INIT,
            "%(modname)s",  /* m_name */
            NULL,           /* m_doc */
            -1,             /* m_size */
            methods,        /* m_methods */
        };
        """
        module = self.import_module(name='foo', body=body)
        assert module.test(True, True) == True

    def test_exception(self):
        import sys
        body = """
        static PyObject* foo_pi(PyObject* self, PyObject *args)
        {
            PyErr_SetString(PyExc_Exception, "moo!");
            return NULL;
        }
        static PyMethodDef methods[] = {
            { "raise_exception", foo_pi, METH_NOARGS },
            { NULL }
        };
        static struct PyModuleDef moduledef = {
            PyModuleDef_HEAD_INIT,
            "%(modname)s",  /* m_name */
            NULL,           /* m_doc */
            -1,             /* m_size */
            methods,        /* m_methods */
        };
        """
        module = self.import_module(name='foo', body=body)
        exc = raises(Exception, module.raise_exception)
        if type(exc.value) is not Exception:
            raise exc.value

        assert str(exc.value) == "moo!"

    def test_refcount(self):
        import sys
        body = """
        static PyObject* foo_pi(PyObject* self, PyObject *args)
        {
            PyObject *true_obj = Py_True;
            Py_ssize_t refcnt = true_obj->ob_refcnt;
            Py_ssize_t refcnt_after;
            Py_INCREF(true_obj);
            Py_INCREF(true_obj);
            if (!PyBool_Check(true_obj))
                Py_RETURN_NONE;
            refcnt_after = true_obj->ob_refcnt;
            Py_DECREF(true_obj);
            Py_DECREF(true_obj);
            fprintf(stderr, "REFCNT %ld %ld\\n", refcnt, refcnt_after);
            return PyBool_FromLong(refcnt_after == refcnt + 2);
        }
        static PyObject* foo_bar(PyObject* self, PyObject *args)
        {
            PyObject *true_obj = Py_True;
            PyObject *tup = NULL;
            Py_ssize_t refcnt = true_obj->ob_refcnt;
            Py_ssize_t refcnt_after;

            tup = PyTuple_New(1);
            Py_INCREF(true_obj);
            if (PyTuple_SetItem(tup, 0, true_obj) < 0)
                return NULL;
            refcnt_after = true_obj->ob_refcnt;
            Py_DECREF(tup);
            fprintf(stderr, "REFCNT2 %ld %ld %ld\\n", refcnt, refcnt_after,
                    true_obj->ob_refcnt);
            return PyBool_FromLong(refcnt_after == refcnt + 1 &&
                                   refcnt == true_obj->ob_refcnt);
        }

        static PyMethodDef methods[] = {
            { "test_refcount", foo_pi, METH_NOARGS },
            { "test_refcount2", foo_bar, METH_NOARGS },
            { NULL }
        };
        static struct PyModuleDef moduledef = {
            PyModuleDef_HEAD_INIT,
            "%(modname)s",  /* m_name */
            NULL,           /* m_doc */
            -1,             /* m_size */
            methods,        /* m_methods */
        };
        """
        module = self.import_module(name='foo', body=body)
        assert module.test_refcount()
        assert module.test_refcount2()


    def test_init_exception(self):
        import sys
        init = """
            PyErr_SetString(PyExc_Exception, "moo!");
            return NULL;
        """
        exc = raises(Exception, self.import_module, name='foo', init=init)
        if type(exc.value) is not Exception:
            raise exc.value

        assert str(exc.value) == "moo!"


    def test_internal_exceptions(self):
        if self.runappdirect:
            skip('cannot import module with undefined functions')
        import sys
        body = """
        PyAPI_FUNC(PyObject*) PyPy_Crash1(void);
        PyAPI_FUNC(Py_ssize_t) PyPy_Crash2(void);
        PyAPI_FUNC(PyObject*) PyPy_Noop(PyObject*);
        static PyObject* foo_crash1(PyObject* self, PyObject *args)
        {
            return PyPy_Crash1();
        }
        static PyObject* foo_crash2(PyObject* self, PyObject *args)
        {
            Py_ssize_t a = PyPy_Crash2();
            if (a == -1)
                return NULL;
            return PyFloat_FromDouble((double)a);
        }
        static PyObject* foo_crash3(PyObject* self, PyObject *args)
        {
            Py_ssize_t a = PyPy_Crash2();
            if (a == -1)
                PyErr_Clear();
            return PyFloat_FromDouble((double)a);
        }
        static PyObject* foo_crash4(PyObject* self, PyObject *args)
        {
            Py_ssize_t a = PyPy_Crash2();
            return PyFloat_FromDouble((double)a);
        }
        static PyObject* foo_noop(PyObject* self, PyObject* args)
        {
            Py_INCREF(args);
            return PyPy_Noop(args);
        }
        static PyObject* foo_set(PyObject* self, PyObject *args)
        {
            PyErr_SetString(PyExc_TypeError, "clear called with no error");
            if (PyLong_Check(args)) {
                Py_INCREF(args);
                return args;
            }
            return NULL;
        }
        static PyObject* foo_clear(PyObject* self, PyObject *args)
        {
            PyErr_Clear();
            if (PyLong_Check(args)) {
                Py_INCREF(args);
                return args;
            }
            return NULL;
        }
        static PyMethodDef methods[] = {
            { "crash1", foo_crash1, METH_NOARGS },
            { "crash2", foo_crash2, METH_NOARGS },
            { "crash3", foo_crash3, METH_NOARGS },
            { "crash4", foo_crash4, METH_NOARGS },
            { "clear",  foo_clear,  METH_O },
            { "set",    foo_set,    METH_O },
            { "noop",   foo_noop,   METH_O },
            { NULL }
        };
        static struct PyModuleDef moduledef = {
            PyModuleDef_HEAD_INIT,
            "%(modname)s",  /* m_name */
            NULL,           /* m_doc */
            -1,             /* m_size */
            methods,        /* m_methods */
        };
        """
        module = self.import_module(name='foo', body=body)

        # uncaught interplevel exceptions are turned into SystemError
        expected1 = "ZeroDivisionError('integer division or modulo by zero',)"
        # win64 uses long internally not int, which gives a different error
        expected2 = "ZeroDivisionError('integer division by zero',)"
        exc = raises(SystemError, module.crash1)
        v = exc.value.args[0]
        assert v == expected1 or v == expected2

        exc = raises(SystemError, module.crash2)
        assert v == expected1 or v == expected2

        # caught exception, api.cpython_api return value works
        assert module.crash3() == -1

        expected = 'An exception was set, but function returned a value'
        # PyPy only incompatibility/extension
        exc = raises(SystemError, module.crash4)
        assert v == expected1 or v == expected2

        # An exception was set by the previous call, it can pass
        # cleanly through a call that doesn't check error state
        assert module.noop(1) == 1

        # clear the exception but return NULL, signalling an error
        expected = 'Function returned a NULL result without setting an exception'
        exc = raises(SystemError, module.clear, None)
        assert exc.value.args[0] == expected

        # Set an exception and return NULL
        raises(TypeError, module.set, None)

        # clear any exception and return a value
        assert module.clear(1) == 1

        # Set an exception, but return non-NULL
        expected = 'An exception was set, but function returned a value'
        exc = raises(SystemError, module.set, 1)
        assert exc.value.args[0] == expected


        # Clear the exception and return a value, all is OK
        assert module.clear(1) == 1

    def test_new_exception(self):
        mod = self.import_extension('foo', [
            ('newexc', 'METH_VARARGS',
             '''
             char *name = _PyUnicode_AsString(PyTuple_GetItem(args, 0));
             return PyErr_NewException(name, PyTuple_GetItem(args, 1),
                                       PyTuple_GetItem(args, 2));
             '''
             ),
            ])
        raises(SystemError, mod.newexc, "name", Exception, {})

    @pytest.mark.skipif(only_pypy, reason='pypy specific test')
    def test_hash_pointer(self):
        mod = self.import_extension('foo', [
            ('get_hash', 'METH_NOARGS',
             '''
             return PyLong_FromLong(_Py_HashPointer(Py_None));
             '''
             ),
            ])
        h = mod.get_hash()
        assert h != 0
        assert h % 4 == 0 # it's the pointer value

    def test_types(self):
        """test the presence of random types"""

        mod = self.import_extension('foo', [
            ('get_names', 'METH_NOARGS',
             '''
             /* XXX in tests, the C type is not correct */
             #define NAME(type) ((PyTypeObject*)&type)->tp_name
             return Py_BuildValue("ssssss",
                                  NAME(PyCell_Type),
                                  NAME(PyModule_Type),
                                  NAME(PyProperty_Type),
                                  NAME(PyStaticMethod_Type),
                                  NAME(PyClassMethod_Type),
                                  NAME(PyCFunction_Type)
                                  );
             '''
             ),
            ])
        assert mod.get_names() == ('cell', 'module', 'property',
                                   'staticmethod', 'classmethod',
                                   'builtin_function_or_method')

    def test_get_programname(self):
        mod = self.import_extension('foo', [
            ('get_programname', 'METH_NOARGS',
             '''
             wchar_t* name1 = Py_GetProgramName();
             wchar_t* name2 = Py_GetProgramName();
             if (name1 != name2)
                 Py_RETURN_FALSE;
             return PyUnicode_FromWideChar(name1, wcslen(name1));
             '''
             )],
            prologue='#include <wchar.h>')
        p = mod.get_programname()
        print(p)
        assert 'py' in p

    @pytest.mark.skipif(only_pypy, reason='pypy only test')
    def test_get_version(self):
        mod = self.import_extension('foo', [
            ('get_version', 'METH_NOARGS',
             '''
             char* name1 = Py_GetVersion();
             char* name2 = Py_GetVersion();
             if (name1 != name2)
                 Py_RETURN_FALSE;
             return PyUnicode_FromString(name1);
             '''
             ),
            ])
        p = mod.get_version()
        print(p)
        assert 'PyPy' in p

    def test_no_double_imports(self):
        import sys, os
        try:
            body = """
            static struct PyModuleDef moduledef = {
                PyModuleDef_HEAD_INIT,
                "%(modname)s",  /* m_name */
                NULL,           /* m_doc */
                -1,             /* m_size */
                NULL            /* m_methods */
            };
            """
            init = """
            static int _imported_already = 0;
            FILE *f = fopen("_imported_already", "w");
            fprintf(f, "imported_already: %d\\n", _imported_already);
            fclose(f);
            _imported_already = 1;
            return PyModule_Create(&moduledef);
            """
            self.import_module(name='foo', init=init, body=body)
            assert 'foo' in sys.modules

            f = open('_imported_already')
            data = f.read()
            f.close()
            assert data == 'imported_already: 0\n'

            f = open('_imported_already', 'w')
            f.write('not again!\n')
            f.close()
            m1 = sys.modules['foo']
            m2 = self.load_module(m1.__file__, name='foo')
            assert m1 is m2
            assert m1 is sys.modules['foo']

            f = open('_imported_already')
            data = f.read()
            f.close()
            assert data == 'not again!\n'

        finally:
            try:
                os.unlink('_imported_already')
            except OSError:
                pass

    def test_no_structmember(self):
        """structmember.h should not be included by default."""
        mod = self.import_extension('foo', [
            ('bar', 'METH_NOARGS',
             '''
             /* reuse a name that is #defined in structmember.h */
             int RO = 0; (void)RO;
             Py_RETURN_NONE;
             '''
             ),
        ])

    def test_consistent_flags(self):
        import sys
        mod = self.import_extension('foo', [
            ('test_optimize', 'METH_NOARGS',
             '''
                return PyLong_FromLong(Py_OptimizeFlag);
             '''),
        ])
        # This is intentionally set to -1 by default from missing.c
        # and should be set to sys.flags.optimize at startup
        assert mod.test_optimize() == sys.flags.optimize
    def test_gc_track(self):
        """
        Test if Py_GC_Track and Py_GC_Untrack are adding and removing container
        objects from the list of all garbage-collected PyObjects.
        """
        if self.runappdirect:
            skip('cannot import module with undefined functions')

        init = """
        if (Py_IsInitialized()) {
            PyObject* m;
            if (PyType_Ready(&FooType) < 0)
                return NULL;
            m = PyModule_Create(&moduledef);
            if (m == NULL)
                return NULL;
            Py_INCREF(&FooType);
            PyModule_AddObject(m, "Foo", (PyObject *)&FooType);
            return m;
        }
        """
        body = """
        #include <Python.h>
        #include "structmember.h"
        typedef struct {
            PyObject_HEAD
        } Foo;
        static PyTypeObject FooType;
        static PyObject* Foo_new(PyTypeObject *type, PyObject *args,
                                   PyObject *kwds)
        {
            Foo *self;
            self = PyObject_GC_New(Foo, type);
            PyObject_GC_Track(self);
            return (PyObject *)self;
        }
        static PyTypeObject FooType = {
            PyVarObject_HEAD_INIT(NULL, 0)
            "foo.Foo",                 /* tp_name */
            sizeof(Foo),               /* tp_basicsize */
            0,                         /* tp_itemsize */
            0,                         /* tp_dealloc */
            0,                         /* tp_print */
            0,                         /* tp_getattr */
            0,                         /* tp_setattr */
            0,                         /* tp_compare */
            0,                         /* tp_repr */
            0,                         /* tp_as_number */
            0,                         /* tp_as_sequence */
            0,                         /* tp_as_mapping */
            0,                         /* tp_hash */
            0,                         /* tp_call */
            0,                         /* tp_str */
            0,                         /* tp_getattro */
            0,                         /* tp_setattro */
            0,                         /* tp_as_buffer */
            Py_TPFLAGS_DEFAULT |
                Py_TPFLAGS_HAVE_GC,    /* tp_flags */
            0,                         /* tp_doc */
            0,                         /* tp_traverse */
            0,                         /* tp_clear */
            0,                         /* tp_richcompare */
            0,                         /* tp_weaklistoffset */
            0,                         /* tp_iter */
            0,                         /* tp_iternext */
            0,                         /* tp_methods */
            0,                         /* tp_members */
            0,                         /* tp_getset */
            0,                         /* tp_base */
            0,                         /* tp_dict */
            0,                         /* tp_descr_get */
            0,                         /* tp_descr_set */
            0,                         /* tp_dictoffset */
            0,                         /* tp_init */
            0,                         /* tp_alloc */
            Foo_new,                   /* tp_new */
            PyObject_GC_Del,           /* tp_free */
        };
                
        static PyObject * Foo_pygchead(PyObject *self, PyObject *foo)
        {
            PyGC_Head * gc_head = _PyPy_pyobj_as_gc((GCHdr_PyObject *)foo);
            return PyLong_FromLong((long)gc_head);
        }
        
        static PyObject * Foo_untrack(PyObject *self, PyObject *foo)
        {
           PyObject_GC_UnTrack(foo);
           Py_RETURN_NONE;
        }
        
        static PyMethodDef module_methods[] = {
            {"pygchead", (PyCFunction)Foo_pygchead, METH_O, ""},
            {"untrack", (PyCFunction)Foo_untrack, METH_O, ""},
            {NULL}  /* Sentinel */
        };
        
        static struct PyModuleDef moduledef = {
                PyModuleDef_HEAD_INIT,
                "%(modname)s",  /* m_name */
                NULL,           /* m_doc */
                -1,             /* m_size */
                module_methods  /* m_methods */
            };
        """
        module = self.import_module(name='foo', init=init, body=body)

        f = module.Foo()
        pygchead = module.pygchead(f)
        result = self.in_pygclist(pygchead)
        assert result

        module.untrack(f)
        result = self.in_pygclist(pygchead)
        assert not result

    def test_gc_collect_simple(self):
        """
        Test if a simple collect is working
        TODO: make more precise
        """
        skip('does not work right now, because of how the test is set up, '
             'see comment below')

        if self.runappdirect:
            skip('cannot import module with undefined functions')

        init = """
        if (Py_IsInitialized()) {
            PyObject* m;
            if (PyType_Ready(&CycleType) < 0)
                return;
            m = Py_InitModule("Cycle", module_methods);
            if (m == NULL)
                return;
            Py_INCREF(&CycleType);
            PyModule_AddObject(m, "Cycle", (PyObject *)&CycleType);
        }
        """

        body = """
        #include <Python.h>
        #include "structmember.h"
        #include <stdio.h>
        #include <signal.h>
        typedef struct {
            PyObject_HEAD
            PyObject *next;
            PyObject *val;
        } Cycle;
        static PyTypeObject CycleType;
        static int Cycle_traverse(Cycle *self, visitproc visit, void *arg)
        {
            printf("traverse begin!\\n");
            int vret;
            if (self->next) {
                vret = visit(self->next, arg);
                if (vret != 0)
                    return vret;
            }
            if (self->val) {
                vret = visit(self->val, arg);
                if (vret != 0)
                    return vret;
            }
            printf("traverse end!\\n");
            return 0;
        }
        static int Cycle_clear(Cycle *self)
        {
            printf("clear!\\n");
            PyObject *tmp;
            tmp = self->next;
            self->next = NULL;
            Py_XDECREF(tmp);
            tmp = self->val;
            self->val = NULL;
            Py_XDECREF(tmp);
            return 0;
        }
        static void Cycle_dealloc(Cycle* self)
        {
            printf("dealloc!\\n");
            PyObject_GC_UnTrack(self);
            Py_TYPE(self)->tp_free((PyObject*)self);
        }
        static PyObject* Cycle_new(PyTypeObject *type, PyObject *args,
                                   PyObject *kwds)
        {
            printf("\\nCycle begin new\\n");
            fflush(stdout);
            Cycle *self;
            self = PyObject_GC_New(Cycle, type);
            if (self != NULL) {
                //self->next = PyString_FromString("");
                //if (self->next == NULL) {
                //    Py_DECREF(self);
                //    return NULL;
                //}
               PyObject_GC_Track(self);
               printf("\\nCycle tracked: %lx\\n", (Py_ssize_t)self);
               printf("\\nCycle refcnt: %lx\\n", (Py_ssize_t)self->ob_refcnt);
               printf("\\nCycle pypy_link: %lx\\n", (Py_ssize_t)self->ob_pypy_link);
               raise(SIGINT);
            } else {
               printf("\\nCycle new null\\n");
            }
            fflush(stdout);
            return (PyObject *)self;
        }
        static int Cycle_init(Cycle *self, PyObject *args, PyObject *kwds)
        {
            PyObject *next=NULL, *tmp;
            static char *kwlist[] = {"next", NULL};
            if (! PyArg_ParseTupleAndKeywords(args, kwds, "|O", kwlist,
                                              &next))
                return -1;
            if (next) {
                tmp = self->next;
                Py_INCREF(next);
                self->next = next;
                Py_XDECREF(tmp);
            }
            return 0;
        }
        static PyMemberDef Cycle_members[] = {
            {"next", T_OBJECT_EX, offsetof(Cycle, next), 0, "next"},
            {"val", T_OBJECT_EX, offsetof(Cycle, val), 0, "val"},
            {NULL}  /* Sentinel */
        };
        static PyMethodDef Cycle_methods[] = {
            {NULL}  /* Sentinel */
        };
        static PyTypeObject CycleType = {
            PyVarObject_HEAD_INIT(NULL, 0)
            "Cycle.Cycle",             /* tp_name */
            sizeof(Cycle),             /* tp_basicsize */
            0,                         /* tp_itemsize */
            (destructor)Cycle_dealloc, /* tp_dealloc */
            0,                         /* tp_print */
            0,                         /* tp_getattr */
            0,                         /* tp_setattr */
            0,                         /* tp_compare */
            0,                         /* tp_repr */
            0,                         /* tp_as_number */
            0,                         /* tp_as_sequence */
            0,                         /* tp_as_mapping */
            0,                         /* tp_hash */
            0,                         /* tp_call */
            0,                         /* tp_str */
            0,                         /* tp_getattro */
            0,                         /* tp_setattro */
            0,                         /* tp_as_buffer */
            Py_TPFLAGS_DEFAULT |
                Py_TPFLAGS_BASETYPE |
                Py_TPFLAGS_HAVE_GC,    /* tp_flags */
            "Cycle objects",           /* tp_doc */
            (traverseproc)Cycle_traverse,   /* tp_traverse */
            (inquiry)Cycle_clear,           /* tp_clear */
            0,                         /* tp_richcompare */
            0,                         /* tp_weaklistoffset */
            0,                         /* tp_iter */
            0,                         /* tp_iternext */
            Cycle_methods,             /* tp_methods */
            Cycle_members,             /* tp_members */
            0,                         /* tp_getset */
            0,                         /* tp_base */
            0,                         /* tp_dict */
            0,                         /* tp_descr_get */
            0,                         /* tp_descr_set */
            0,                         /* tp_dictoffset */
            (initproc)Cycle_init,      /* tp_init */
            0,                         /* tp_alloc */
            Cycle_new,                 /* tp_new */
            PyObject_GC_Del,           /* tp_free */
        };
        
        static Cycle *c;
        static PyObject * Cycle_cc(Cycle *self, PyObject *val)
        {
            c = PyObject_GC_New(Cycle, &CycleType);
            if (c == NULL)
                return NULL;
            PyObject_GC_Track(c);
            Py_INCREF(val);
            c->val = val;                // set value
            Py_INCREF(c);
            c->next = (PyObject *)c;     // create self reference
            Py_INCREF(Py_None);
            return Py_None;
        }
        static PyObject * Cycle_cd(Cycle *self)
        {
            Py_DECREF(c);                // throw cycle away
            Py_INCREF(Py_None);
            return Py_None;
        }
        static PyMethodDef module_methods[] = {
            {"createCycle", (PyCFunction)Cycle_cc, METH_OLDARGS, ""},
            {"discardCycle", (PyCFunction)Cycle_cd, METH_NOARGS, ""},
            {NULL}  /* Sentinel */
        };
        """

        module = self.import_module(name='Cycle', init=init, body=body)

        # TODO: The code below will fail as soon as the host GC kicks in the
        # test uses the rawrefcount module for object <-> pyobject linking,
        # which currently sets an invalid pointer to the object in the
        # pyobject's header, which in turn causes the GC to crash (because
        # it currently assumes any non-null pointer is a valid pointer and
        # tries to follow it). Even with debug_collect.
        #
        #       Solutions - A: set a valid pointer in rawrefcount (best)
        #                 - B: set a special pointer in rawrefcount,
        #                      which will be detected as such in the GC and
        #                        1) ... handled correctly
        #                        2) ... always be kept -> floating garbage
        #
        # Note: As we use the GC of the host, that is running the tests,
        # running it on CPython or any other version of PyPy might lead to
        # different results.
        module.Cycle()
        self.debug_collect()