#include "Constants.h"
#include "Source/Source.h"
#include "TaintRange/TaintRange.h"
#include "TaintedObject/TaintedObject.h"

static PyMethodDef TaintTrackingMethods[] = {
    // We are using  METH_VARARGS because we need compatibility with
    // python 3.5, 3.6. but METH_FASTCALL could be used instead for python
    // >= 3.7
    {"setup", (PyCFunction)setup, METH_VARARGS, "setup tainting module"},
    {"new_pyobject_id", (PyCFunction)new_pyobject_id, METH_VARARGS,
     "new_pyobject_id"},
    {nullptr, nullptr, 0, nullptr}};

static struct PyModuleDef taint_tracking = {
    PyModuleDef_HEAD_INIT,
    .m_name = PY_MODULE_NAME,
    .m_doc = "Taint tracking module.",
    .m_size = -1,
    .m_methods = TaintTrackingMethods};

PyMODINIT_FUNC PyInit__native(void) {
  PyObject *m;

  if (PyType_Ready(&TaintRangeType) < 0)
    return nullptr;

  if (PyType_Ready(&SourceType) < 0)
    return nullptr;

  m = PyModule_Create(&taint_tracking);
  if (m == nullptr)
    return nullptr;

  Py_INCREF(&TaintRangeType);
  if (PyModule_AddObject(m, "TaintRange", (PyObject *)&TaintRangeType) < 0) {
    Py_DECREF(&TaintRangeType);
    Py_DECREF(m);
    return nullptr;
  }

  Py_INCREF(&SourceType);
  if (PyModule_AddObject(m, "Source", (PyObject *)&SourceType) < 0) {
    Py_DECREF(&SourceType);
    Py_DECREF(m);
    return nullptr;
  }

  return m;
}
