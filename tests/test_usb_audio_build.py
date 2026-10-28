"""Exercise PlatformIO's stock-audio exclusion without modifying an SDK.

The project supplies TinyUSB's global UAC callbacks. Only the installed
Arduino USBAudioCard implementation must be excluded; project/vendor files
with the same name and the remaining USB classes must still compile.
"""
import fnmatch
import importlib.util
from pathlib import Path
import tempfile
import unittest


ROOT=Path(__file__).resolve().parents[1]
SPEC=importlib.util.spec_from_file_location("usb_audio_build_patch",ROOT/"tools/usb_fs_patch.py")
PATCH=importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PATCH)


class SourceNode:
    def __init__(self,path):
        self.path=Path(path)

    def srcnode(self):
        return self

    def get_abspath(self):
        return str(self.path.absolute())

    def get_path(self):
        return str(self.path)


class BuildNode:
    """SCons can hand the middleware a derived node with a different path."""
    def __init__(self,source):
        self.source=SourceNode(source)

    def srcnode(self):
        return self.source

    def get_abspath(self):
        return "/unrelated/build/output.o"


class Environment:
    def __init__(self,root):
        self.root=root
        self.framework=root/"framework-arduinoespressif32"
        self.framework_libs=root/"framework-arduinoespressif32-libs"
        self.middlewares=[]
        self.appended={}

    def PioPlatform(self):
        return self

    def get_package_dir(self,name):
        return str({"framework-arduinoespressif32":self.framework,
                    "framework-arduinoespressif32-libs":self.framework_libs}[name])

    def BoardConfig(self):
        return {"build.mcu":"esp32p4","build.chip_variant":"esp32p4_es"}

    def Append(self,**values):
        for key,value in values.items():
            self.appended.setdefault(key,[]).extend(value)

    def subst(self,value):
        return value.replace("$PROJECT_DIR",str(ROOT)).replace("$BUILD_DIR",str(self.root/"build"))

    def AddBuildMiddleware(self,callback,pattern=None):
        self.middlewares.append((callback,pattern))

    def fixture(self):
        fs=self.framework/"libraries/FS/src/FS.h"
        fs.parent.mkdir(parents=True)
        fs.write_text("// fixture FS header\n",encoding="utf-8")
        options=self.framework_libs/"esp32p4_es/include/arduino_tinyusb/tinyusb/src/tusb_option.h"
        options.parent.mkdir(parents=True)
        options.write_text("#define TUSB_VERSION_MAJOR 0\n#define TUSB_VERSION_MINOR 21\n"
                           "#define TUSB_VERSION_REVISION 0\n",encoding="utf-8")
        stock=self.framework/"libraries/USB/src/USBAudioCard.cpp"
        stock.parent.mkdir(parents=True)
        stock.write_text("// installed SDK source remains untouched\n",encoding="utf-8")
        return stock


class StockAudioBuildTests(unittest.TestCase):
    def test_registered_middleware_excludes_only_exact_stock_source(self):
        with tempfile.TemporaryDirectory(prefix="tab5-usb-audio-build-") as directory:
            environment=Environment(Path(directory))
            stock=environment.fixture()
            original=stock.read_bytes()
            PATCH.register_platformio(environment)
            entries=[entry for entry in environment.middlewares if entry[1]=="*USBAudioCard.cpp"]
            self.assertEqual(len(entries),1)
            callback,pattern=entries[0]
            self.assertIs(callback,PATCH.exclude_stock_audio)
            stock_node=BuildNode(stock)
            self.assertTrue(fnmatch.fnmatch(stock_node.srcnode().get_path(),pattern))
            self.assertIsNone(callback(environment,stock_node))
            self.assertEqual(stock.read_bytes(),original)
            # No source writes/deletions and no generated replacement unit.
            self.assertFalse((environment.root/"build").exists())

            for path in (environment.root/"project/src/USBAudioCard.cpp",
                         environment.root/"vendor/USB/src/USBAudioCard.cpp",
                         environment.framework/"libraries/Other/src/USBAudioCard.cpp",
                         environment.framework/"libraries/USB/src/USBHID.cpp",
                         environment.framework/"libraries/USB/src/USBCDC.cpp",
                         environment.framework/"libraries/USB/src/USB.cpp",
                         environment.framework/"cores/esp32/esp32-hal-tinyusb.c"):
                with self.subTest(path=path):
                    node=BuildNode(path)
                    self.assertIs(callback(environment,node),node)

            # Existing full-speed CDC/HID/core replacement hooks remain present.
            self.assertEqual([pattern for _,pattern in environment.middlewares],
                             ["*esp32-hal-tinyusb.c","*USBHID.cpp","*USBCDC.cpp","*USBAudioCard.cpp"])
            self.assertIn(str(environment.framework/"libraries/FS/src"),environment.appended["CPPPATH"])

    def test_canonical_source_alias_is_excluded_but_other_sdk_is_retained(self):
        with tempfile.TemporaryDirectory(prefix="tab5-usb-audio-path-") as directory:
            environment=Environment(Path(directory))
            stock=environment.fixture()
            alias=environment.root/"sdk-alias"
            alias.symlink_to(environment.framework,target_is_directory=True)
            through_alias=SourceNode(alias/"libraries/USB/src/USBAudioCard.cpp")
            self.assertIsNone(PATCH.exclude_stock_audio(environment,through_alias))
            normalized=SourceNode(stock.parent/".."/"src"/"USBAudioCard.cpp")
            self.assertIsNone(PATCH.exclude_stock_audio(environment,normalized))
            other_sdk=SourceNode(environment.root/"other-sdk/libraries/USB/src/USBAudioCard.cpp")
            self.assertIs(PATCH.exclude_stock_audio(environment,other_sdk),other_sdk)


if __name__=="__main__":
    unittest.main()
