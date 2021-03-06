# Copyright 2014-present PlatformIO <contact@platformio.org>
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import hashlib
import json
import os
import uuid
from copy import deepcopy
from os import environ, getenv, listdir, remove
from os.path import dirname, getmtime, isdir, isfile, join
from time import time

from lockfile import LockFile

from platformio import __version__, util
from platformio.exception import InvalidSettingName, InvalidSettingValue

DEFAULT_SETTINGS = {
    "check_platformio_interval": {
        "description": "Check for the new PlatformIO interval (days)",
        "value": 3
    },
    "check_platforms_interval": {
        "description": "Check for the platform updates interval (days)",
        "value": 7
    },
    "check_libraries_interval": {
        "description": "Check for the library updates interval (days)",
        "value": 7
    },
    "auto_update_platforms": {
        "description": "Automatically update platforms (Yes/No)",
        "value": False
    },
    "auto_update_libraries": {
        "description": "Automatically update libraries (Yes/No)",
        "value": False
    },
    "force_verbose": {
        "description": "Force verbose output when processing environments",
        "value": False
    },
    "enable_ssl": {
        "description": "Enable SSL for PlatformIO Services",
        "value": False
    },
    "enable_telemetry": {
        "description":
        ("Telemetry service <http://docs.platformio.org/en/stable/"
         "userguide/cmd_settings.html?#enable-telemetry> (Yes/No)"),
        "value": True
    }
}

SESSION_VARS = {"command_ctx": None, "force_option": False, "caller_id": None}


class State(object):

    def __init__(self, path=None, lock=False):
        self.path = path
        self.lock = lock
        if not self.path:
            self.path = join(util.get_home_dir(), "appstate.json")
        self._state = {}
        self._prev_state = {}
        self._lockfile = None

    def __enter__(self):
        try:
            self._lock_state_file()
            if isfile(self.path):
                self._state = util.load_json(self.path)
        except ValueError:
            self._state = {}
        self._prev_state = deepcopy(self._state)
        return self._state

    def __exit__(self, type_, value, traceback):
        if self._prev_state != self._state:
            with open(self.path, "w") as fp:
                if "dev" in __version__:
                    json.dump(self._state, fp, indent=4)
                else:
                    json.dump(self._state, fp)
        self._unlock_state_file()

    def _lock_state_file(self):
        if not self.lock:
            return
        self._lockfile = LockFile(self.path)

        if self._lockfile.is_locked() and \
                (time() - getmtime(self._lockfile.lock_file)) > 10:
            self._lockfile.break_lock()

        self._lockfile.acquire()

    def _unlock_state_file(self):
        if self._lockfile:
            self._lockfile.release()


class LocalCache(object):

    def __init__(self, cache_dir=None):
        self.cache_dir = cache_dir or join(util.get_home_dir(), ".cache")
        if not self.cache_dir:
            os.makedirs(self.cache_dir)
        self.db_path = join(self.cache_dir, "db.data")

    def __enter__(self):
        if not isfile(self.db_path):
            return self
        newlines = []
        found = False
        with open(self.db_path) as fp:
            for line in fp.readlines():
                if "=" not in line:
                    continue
                line = line.strip()
                expire, path = line.split("=")
                if time() < int(expire):
                    newlines.append(line)
                    continue
                found = True
                if isfile(path):
                    remove(path)
                    if not len(listdir(dirname(path))):
                        util.rmtree_(dirname(path))
        if found:
            with open(self.db_path, "w") as fp:
                fp.write("\n".join(newlines) + "\n")
        return self

    def __exit__(self, type_, value, traceback):
        pass

    def get_cache_path(self, key):
        assert len(key) > 3
        return join(self.cache_dir, key[-2:], key)

    @staticmethod
    def key_from_args(*args):
        h = hashlib.md5()
        for data in args:
            h.update(str(data))
        return h.hexdigest()

    def get(self, key):
        cache_path = self.get_cache_path(key)
        if not isfile(cache_path):
            return None
        with open(cache_path) as fp:
            data = fp.read()
            if data[0] in ("{", "["):
                return json.loads(data)
            return data

    def set(self, key, data, valid):
        if not data:
            return
        tdmap = {"s": 1, "m": 60, "h": 3600, "d": 86400}
        assert valid.endswith(tuple(tdmap.keys()))
        cache_path = self.get_cache_path(key)
        if not isdir(dirname(cache_path)):
            os.makedirs(dirname(cache_path))
        with open(cache_path, "w") as fp:
            if isinstance(data, dict) or isinstance(data, list):
                json.dump(data, fp)
            else:
                fp.write(str(data))
        expire_time = int(time() + tdmap[valid[-1]] * int(valid[:-1]))
        with open(self.db_path, "w+") as fp:
            fp.write("%s=%s\n" % (str(expire_time), cache_path))
        return True

    def clean(self):
        if isdir(self.cache_dir):
            util.rmtree_(self.cache_dir)


def sanitize_setting(name, value):
    if name not in DEFAULT_SETTINGS:
        raise InvalidSettingName(name)

    defdata = DEFAULT_SETTINGS[name]
    try:
        if "validator" in defdata:
            value = defdata['validator']()
        elif isinstance(defdata['value'], bool):
            if not isinstance(value, bool):
                value = str(value).lower() in ("true", "yes", "y", "1")
        elif isinstance(defdata['value'], int):
            value = int(value)
    except Exception:
        raise InvalidSettingValue(value, name)
    return value


def get_state_item(name, default=None):
    with State() as data:
        return data.get(name, default)


def set_state_item(name, value):
    with State(lock=True) as data:
        data[name] = value


def get_setting(name):
    _env_name = "PLATFORMIO_SETTING_%s" % name.upper()
    if _env_name in environ:
        return sanitize_setting(name, getenv(_env_name))

    with State() as data:
        if "settings" in data and name in data['settings']:
            return data['settings'][name]

    return DEFAULT_SETTINGS[name]['value']


def set_setting(name, value):
    with State(lock=True) as data:
        if "settings" not in data:
            data['settings'] = {}
        data['settings'][name] = sanitize_setting(name, value)


def reset_settings():
    with State(lock=True) as data:
        if "settings" in data:
            del data['settings']


def get_session_var(name, default=None):
    return SESSION_VARS.get(name, default)


def set_session_var(name, value):
    assert name in SESSION_VARS
    SESSION_VARS[name] = value


def is_disabled_progressbar():
    return any([get_session_var("force_option"), util.is_ci(),
                getenv("PLATFORMIO_DISABLE_PROGRESSBAR") == "true"])


def get_cid():
    cid = get_state_item("cid")
    if not cid:
        cid = str(
            uuid.UUID(bytes=hashlib.md5(
                str(getenv("C9_UID")
                    if getenv("C9_UID") else uuid.getnode())).digest()))
        set_state_item("cid", cid)
    return cid
