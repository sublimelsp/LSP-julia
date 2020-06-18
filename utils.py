import os
import sublime


def merge_dicts(default: dict, user: dict):
    """ Recursively merges user into default """
    for key, value in default.items():
        if key in user:
            new_value = user[key]
            if isinstance(value, dict) and isinstance(new_value, dict):
                yield key, dict(merge_dicts(value, new_value))
            # elif isinstance(value, list) and isinstance(new_value, list):
            #     yield key, list(set(value) | set(new_value))
            else:
                # overwrite default value with user value
                yield key, new_value
        else:
            yield key, value
    # add all additional items from user
    for key, value in user.items():
        if key not in default:
            yield key, value


def load_settings(base_name: str) -> dict:
    settings = {"enabled": True}

    default_settings_json = sublime.load_resource("Packages/{}/{}".format(__package__, base_name))
    default_settings = sublime.decode_value(default_settings_json)

    if os.path.exists("{}/User/{}".format(sublime.packages_path(), base_name)):
        user_settings_json = sublime.load_resource("Packages/User/{}".format(base_name))
        user_settings = sublime.decode_value(user_settings_json)
        for key, value in merge_dicts(default_settings, user_settings):
            settings[key] = value
    else:
        for key, value in default_settings.items():
            settings[key] = value

    return settings
