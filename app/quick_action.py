"""Quick Action «Сделать субтитры» в ~/Library/Services (Finder → Быстрые действия)."""
import plistlib
import subprocess
import uuid
from pathlib import Path

NAME = "Сделать субтитры"


def install(script: str) -> Path:
    """script — команда zsh, получает выбранные файлы в "$@"."""
    dst = Path.home() / "Library/Services" / f"{NAME}.workflow"
    info = {
        "NSServices": [{
            "NSMenuItem": {"default": NAME},
            "NSMessage": "runWorkflowAsService",
            "NSRequiredContext": {"NSApplicationIdentifier": "com.apple.finder"},
            "NSSendFileTypes": ["public.movie", "public.audio"],
        }]
    }

    action = {
        "AMAccepts": {"Container": "List", "Optional": True, "Types": ["com.apple.cocoa.string"]},
        "AMActionVersion": "2.0.3",
        "AMApplication": ["Automator"],
        "AMParameterProperties": {k: {} for k in
                                  ("COMMAND_STRING", "CheckedForUserDefaultShell", "inputMethod",
                                   "shell", "source")},
        "AMProvides": {"Container": "List", "Types": ["com.apple.cocoa.string"]},
        "ActionBundlePath": "/System/Library/Automator/Run Shell Script.action",
        "ActionName": "Run Shell Script",
        "ActionParameters": {"COMMAND_STRING": script, "CheckedForUserDefaultShell": True,
                             "inputMethod": 1, "shell": "/bin/zsh", "source": ""},
        "BundleIdentifier": "com.apple.RunShellScript",
        "CFBundleVersion": "2.0.3",
        "CanShowSelectedItemsWhenRun": False,
        "CanShowWhenRun": True,
        "Category": ["AMCategoryUtilities"],
        "Class Name": "RunShellScriptAction",
        "InputUUID": str(uuid.uuid4()).upper(),
        "OutputUUID": str(uuid.uuid4()).upper(),
        "UUID": str(uuid.uuid4()).upper(),
        "UnlocalizedApplications": ["Automator"],
        "isViewVisible": 1,
    }

    wflow = {
        "AMApplicationBuild": "523",
        "AMApplicationVersion": "2.10",
        "AMDocumentVersion": "2",
        "actions": [{"action": action, "isViewVisible": 1}],
        "connectors": {},
        "workflowMetaData": {
            "applicationBundleIDsByPath": {},
            "applicationPaths": [],
            "inputTypeIdentifier": "com.apple.Automator.fileSystemObject",
            "outputTypeIdentifier": "com.apple.Automator.nothing",
            "presentationMode": 15,
            "processesInput": 0,
            "serviceApplicationBundleID": "com.apple.finder",
            "serviceApplicationPath": "/System/Library/CoreServices/Finder.app",
            "serviceInputTypeIdentifier": "com.apple.Automator.fileSystemObject",
            "serviceOutputTypeIdentifier": "com.apple.Automator.nothing",
            "serviceProcessesInput": 0,
            "systemImageName": "NSActionTemplate",
            "useAutomaticInputType": 0,
            "workflowTypeIdentifier": "com.apple.Automator.servicesMenu",
        },
    }

    # перезаписываем файлы, ничего не удаляя
    (dst / "Contents").mkdir(parents=True, exist_ok=True)
    with open(dst / "Contents/Info.plist", "wb") as f:
        plistlib.dump(info, f)
    with open(dst / "Contents/document.wflow", "wb") as f:
        plistlib.dump(wflow, f)
    subprocess.run(["/System/Library/CoreServices/pbs", "-update"], capture_output=True)
    return dst
