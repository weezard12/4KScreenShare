def pre_find_module_path(hook_api):
    # Keep the normal search path intact even if PyInstaller cannot probe Tcl/Tk
    # in its isolated subprocess. We bundle Tcl/Tk explicitly in screenshare.spec.
    return
