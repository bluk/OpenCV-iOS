#!/usr/bin/env python

import atexit
import glob
import os
import re
import shutil
import sys
import tempfile
import textwrap

from fabric.api import env, local, hide
from fabric.context_managers import lcd, settings, shell_env
from fabric.contrib.console import confirm
from fabric.contrib.files import exists
from fabric.decorators import runs_once
from fabric.utils import abort
from fabric import colors

# --- Configuration ---------------------------------------------------------

env.verbose = False
env.developer_dir = local("xcode-select -p", capture=True)

# --- Tasks -----------------------------------------------------------------


def verbose(be_verbose=True):
    """
    Makes all following tasks more verbose.
    """
    env.verbose = be_verbose

def _version_str(show_dirty=False):
    git_describe_cmd = "git describe --match='iOS_[0-9]*.[0-9]*' --tags --always --dirty"
    version_str = local(git_describe_cmd, capture=True).strip()[4:]
    if not show_dirty:
        version_str = version_str.replace('-dirty', '')
    return version_str

def developer_dir(dir):
    """
    Sets DEVELOPER_DIR environment variable to correct Xcode
    For example, `fab developer_dir:"/Applications/Xcode6.2.app"
    """
    if os.path.exists(dir):
        env.developer_dir = dir
    else:
        print(colors.red("{dir} is not a valid path".format(dir=dir), bold=True))
        sys.exit(1)


def build(outdir=None, device_sdk=None, simulator_sdk=None, **kwargs):
    """
    Build OpenCV libraries.
    """
    print(colors.white("Setup", bold=True))

    to_hide = [] if env.verbose else ["stdout", "stderr", "running"]

    xcode_preprocessor_flags = {}

    if not outdir:
        message = """
                     You must provide outdir=<sdk output parent dir>
                     Example usage:
                       `fab build:outdir=~` - normal build
                  """
        abort(textwrap.dedent(message).format(**locals()))

    outdir = os.path.abspath(os.path.expanduser(outdir))
    print colors.yellow("Will save release sdk to {outdir}".format(outdir=outdir))
    out_subdir = "opencv_ios_sdk_{0}".format(_version_str(show_dirty=True))

    xcode_preprocessor_flags.update(kwargs)
    formatted_xcode_preprocessor_flags = " ".join("{k}={v}".format(k=k, v=v) for k, v in xcode_preprocessor_flags.iteritems())
    extra_xcodebuild_settings = "GCC_PREPROCESSOR_DEFINITIONS='$(value) {formatted_xcode_preprocessor_flags}'".format(**locals())

    device_sdk = device_sdk or "iphoneos"
    simulator_sdk = simulator_sdk or "iphonesimulator"

    arch_to_sdk = (
                   ("i386", simulator_sdk),
                   ("x86_64", simulator_sdk)
                  )
    schemes_and_targets = (
      ("opencv_core", "opencv_core"),
      ("opencv_imgproc", "opencv_imgproc")
    )

    with settings(hide(*to_hide)):
        opencv_root = local("git rev-parse --show-toplevel", capture=True)

    temp_dir = tempfile.mkdtemp() + os.sep
    # atexit.register(shutil.rmtree, temp_dir, True)

    print(colors.white("Building", bold=True))
    print(colors.white("Using temp dir {temp_dir}".format(**locals())))
    print(colors.white("Using extra Xcode flags: {formatted_xcode_preprocessor_flags}".format(**locals())))
    print(colors.white("Using developer directory: {}".format(env.developer_dir)))

    with lcd(opencv_root + "/OpenCV-iOS"):
        with shell_env(DEVELOPER_DIR=env.developer_dir):
            with settings(hide(*to_hide)):
                out_subdir_suffix = "_".join("{k}-{v}".format(k=k, v=v) for k, v in kwargs.iteritems())
                if out_subdir_suffix:
                    out_subdir_suffix = "_" + out_subdir_suffix
                out_subdir += out_subdir_suffix
                sdk_dir = os.path.join(outdir, out_subdir)

                print(colors.white("Assembling release SDK in {sdk_dir}".format(sdk_dir=sdk_dir), bold=True))
                if os.path.isdir(sdk_dir):
                    shutil.rmtree(sdk_dir)
                opencv_dir = os.path.join(sdk_dir, "OpenCV")
                os.makedirs(opencv_dir)

                for scheme, target in schemes_and_targets:
                    lipo_build_dirs = {}
                    build_config = "Release"
                    arch_build_dirs = {}
                    libname = "lib" + target + ".a"

                    # Build the Archive release
                    print(colors.blue("({build_config}) Building Archive (arm* architectures specified in build config)".format(**locals())))
                    base_xcodebuild_command = "xcrun xcodebuild -scheme {scheme} -target {target} -configuration {build_config}".format(**locals())
                    clean_cmd =  "{base_xcodebuild_command} clean".format(**locals())
                    local(clean_cmd)

                    build_dir = os.path.join(temp_dir, build_config, scheme, target, "Archive")
                    arch_build_dirs["archive"] = build_dir
                    os.makedirs(build_dir)
                    build_cmd = "{base_xcodebuild_command} archive CONFIGURATION_BUILD_DIR={build_dir}".format(**locals())
                    local(build_cmd)

                    # Build the libraries for architectures not specified in the original archive (i386/x86_64)
                    for arch, sdk in arch_to_sdk:
                        print(colors.blue("({build_config}) Building {arch}".format(**locals())))

                        base_xcodebuild_command = "xcrun xcodebuild VALID_ARCHS=\"{arch}\" OTHER_CFLAGS=\"-fembed-bitcode\" -scheme {scheme} -target {target} -arch {arch} -sdk {sdk} -configuration {build_config}".format(**locals())

                        clean_cmd =  "{base_xcodebuild_command} clean".format(**locals())
                        local(clean_cmd)

                        build_dir = os.path.join(temp_dir, build_config, scheme, target, arch)
                        arch_build_dirs[arch] = build_dir
                        os.makedirs(build_dir)
                        build_cmd = "{base_xcodebuild_command} CONFIGURATION_BUILD_DIR={build_dir} {extra_xcodebuild_settings}".format(**locals())
                        local(build_cmd)

                    print(colors.blue("({build_config}) Lipoing".format(**locals())))
                    lipo_dir = os.path.join(temp_dir, build_config, scheme, target, "universal")
                    lipo_build_dirs[build_config] = lipo_dir
                    os.makedirs(lipo_dir)
                    arch_build_dirs["universal"] = lipo_dir
                    lipo_cmd = "xcrun lipo " \
                               "           {archive}/{libname}" \
                               "           -arch i386 {i386}/{libname}" \
                               "           -arch x86_64 {x86_64}/{libname}" \
                               "           -create" \
                               "           -output {universal}/{libname}".format(libname=libname, **arch_build_dirs)
                    local(lipo_cmd)

                    print(colors.blue("({build_config}) Stripping debug symbols".format(**locals())))
                    strip_cmd = "xcrun strip -S {universal}/{libname}".format(libname=libname, **arch_build_dirs)
                    local(strip_cmd)

                    libfile = os.path.join(lipo_build_dirs["Release"], libname)
                    shutil.copy2(libfile, opencv_dir)
