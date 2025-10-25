# This file is currently unused because it requires significant changes due to internal API changes for the TestItem
# feature in LanguageServer.jl

from __future__ import annotations
from LSP.plugin import LspWindowCommand
from LSP.plugin import parse_uri
from LSP.plugin.core.protocol import DocumentUri
from LSP.plugin.core.protocol import Location
from LSP.plugin.core.protocol import Point
from LSP.plugin.core.protocol import URI
from LSP.plugin.core.typing import StrEnum
from LSP.plugin.core.views import point_to_offset
from LSP.plugin.core.views import range_to_region
from functools import partial
from typing import Any, Dict, List, Set, Tuple, TypedDict
from typing import cast
from typing_extensions import NotRequired
from urllib.parse import parse_qs, urldefrag
import html
import itertools
import json
import os
import threading
import traceback
import sublime
import subprocess


class GetTestEnvRequestParams(TypedDict):
    uri: URI

class GetTestEnvRequestParamsReturn(TypedDict):
    packageName: NotRequired[str]
    packageUri: NotRequired[URI]
    projectUri: NotRequired[URI]
    envContentHash: NotRequired[str]

# https://github.com/julia-vscode/julia-vscode/blob/main/src/testing/testFeature.ts
# https://github.com/julia-vscode/julia-vscode/blob/main/scripts/packages/VSCodeTestServer/src/testserver_protocol.jl
class TestserverRunTestitemRequestParams(TypedDict):
    uri: str
    name: str
    packageName: str
    useDefaultUsings: bool
    line: int
    column: int
    code: str

class TestMessage(TypedDict):
    message: str
    location: Location | None

class TestserverRunTestitemRequestParamsReturn(TypedDict):
    status: str
    message: List[TestMessage] | None
    duration: float | None

# Parameters for the runtestitem.jl script, which also requires project_path and package_path
class TestserverRunTestitemRequestExtendedParams(TypedDict):
    uri: str
    name: str
    packageName: str
    useDefaultUsings: bool
    line: int
    column: int
    code: str
    project_path: str
    package_path: str


class TestItemStatus(StrEnum):
    # Used as response values by VSCodeTestServer.jl
    Passed = 'passed'
    Failed = 'failed'
    Errored = 'errored'
    # Additional status indicators used for the different annotations in the view
    Undetermined = 'undetermined'
    Pending = 'pending'
    Invalid = 'invalid'


# sublime.Kind tuples for the "Run Testitem" QuickPanelItems
KIND_PASSED = (sublime.KIND_ID_COLOR_GREENISH, "✓", "Passed")
KIND_FAILED = (sublime.KIND_ID_COLOR_REDISH, "✗", "Failed")
KIND_ERRORED = (sublime.KIND_ID_COLOR_REDISH, "✗", "Errored")

TESTITEM_ICONS: Dict[TestItemStatus, str] = {
    TestItemStatus.Passed: 'Packages/LSP-julia/icons/passed.png',
    TestItemStatus.Failed: 'Packages/LSP-julia/icons/failed.png',
    TestItemStatus.Errored: 'Packages/LSP/icons/error.png',
    TestItemStatus.Undetermined: '',
    TestItemStatus.Pending: 'Packages/LSP-julia/icons/stopwatch.png',
    TestItemStatus.Invalid: ''
}

TESTITEM_SCOPES: Dict[TestItemStatus, str] = {
    TestItemStatus.Passed: 'region.greenish markup.testitem.passed.lsp',
    TestItemStatus.Failed: 'region.redish markup.error markup.testitem.failed.lsp',
    TestItemStatus.Errored: 'region.redish markup.error markup.testitem.errored.lsp',
    TestItemStatus.Undetermined: 'region.cyanish markup.testitem.undetermined.lsp',
    TestItemStatus.Pending: 'region.yellowish markup.testitem.pending.lsp',
    TestItemStatus.Invalid: 'region.redish markup.error markup.testitem.invalid.lsp'
}

TESTITEM_KINDS: Dict[TestItemStatus, Tuple[int, str, str]] = {
    TestItemStatus.Passed: KIND_PASSED,
    TestItemStatus.Failed: KIND_FAILED,
    TestItemStatus.Errored: KIND_ERRORED
}


class TestItemStorage:

    def __init__(self, window: sublime.Window) -> None:
        self.window = window
        self.pending_result = False
        self.testitemparams: Dict[str, Dict[str, Any]] = {}
        self.testitemdetails: Dict[str, List[TestItemDetail]] = {}
        self.testerrordetails: Dict[str, List[TestErrorDetail]] = {}
        self.testitemstatus: Dict[str, List[TestserverRunTestitemRequestParamsReturn]] = {}
        self.error_keys: Dict[str, Set[str]] = {}

    def update(self, uri: DocumentUri, params: PublishTestsParams) -> None:
        # Use the filepath instead of the URI as the key for storing the testitems, because on Windows the language
        # server sometimes uses uppercase and sometimes lowercase drive letters in the URI for the same file.
        filepath = parse_uri(uri)[1]
        testitems = params['testitemdetails']
        testerrors = params['testerrordetails']
        old_params = self.testitemparams.get(filepath)
        if not testitems and not testerrors:
            # If there are no testitems reported, just delete the key for the previously stored items if they existed.
            if old_params:
                del self.testitemparams[filepath]
                del self.testitemdetails[filepath]
                del self.testerrordetails[filepath]
                del self.testitemstatus[filepath]
                self.render_testitems(uri)
            return
        status: List[TestserverRunTestitemRequestParamsReturn] = [{
            'status': TestItemStatus.Undetermined,
            'message': None,
            'duration': None
        } for _ in testitems]
        if not old_params or \
            any(old_params[key] != params[key] for key in ('project_path', 'package_path', 'package_name')):
            # If there were no testitems for this file already stored, or one of the major parameters changed, copy the
            # new parameters and testitems.
            self.testitemparams[filepath] = {
                'uri': params['uri'],
                'version': params.get('version', 0),
                'project_path': params['project_path'],
                'package_path': params['package_path'],
                'package_name': params['package_name']
            }
        else:
            # If there are both new and old testitems, compare them and determine the unchanged items so that the old
            # status is not forgotten. An old and a new testitem is considered the same if it has the same "id".
            # Unfortunately the "id" field for the testitems is not necessarily unique, so there might be incorrect
            # matches via this approach. Perhaps it should be considered as an additional requirement that the "code"
            # property must also be the same (that would mean that testitems lose their status whenever there are
            # changes in the particular testitem code)...
            self.testitemparams[filepath]['uri'] = params['uri']
            self.testitemparams[filepath]['version'] = \
                params.get('version', self.testitemparams[filepath]['version'] + 1)
            for old_idx, old_item in enumerate(self.testitemdetails[filepath]):
                for new_idx, new_item in enumerate(testitems):
                    if old_item['id'] == new_item['id']:
                        # Copy old status into new status for this testitem
                        status[new_idx] = self.testitemstatus[filepath][old_idx]
                        break
        self.testitemdetails[filepath] = testitems
        self.testerrordetails[filepath] = testerrors
        self.testitemstatus[filepath] = status
        self.render_testitems(uri)

    def stored_version(self, uri: DocumentUri) -> int | None:
        filepath = parse_uri(uri)[1]
        params = self.testitemparams.get(filepath)
        if params:
            return params['version']
        return None

    def render_testitems(self, uri: DocumentUri, new_result_idx: int | None = None) -> None:
        filepath = parse_uri(uri)[1]
        view = self.window.find_open_file(filepath)  # This doesn't work if the tab was dragged out of the window...
        if view and not view.is_loading():
            regions_by_status: Dict[TestItemStatus, List[sublime.Region]] = {
                TestItemStatus.Passed: [],
                TestItemStatus.Failed: [],
                TestItemStatus.Errored: [],
                TestItemStatus.Undetermined: [],
                TestItemStatus.Pending: [],
                TestItemStatus.Invalid: []
            }
            if filepath not in self.testitemdetails:
                for status in regions_by_status:
                    view.erase_regions('lsp_julia_testitem_{}'.format(status))
                return
            annotations: Dict[TestItemStatus, List[str]] = {
                TestItemStatus.Passed: [],
                TestItemStatus.Failed: [],
                TestItemStatus.Errored: [],
                TestItemStatus.Undetermined: [],
                TestItemStatus.Pending: [],
                TestItemStatus.Invalid: []
            }
            version = self.testitemparams[filepath]['version']
            error_annotation_color = cast(
                str, view.style_for_scope(TESTITEM_SCOPES[TestItemStatus.Errored])['foreground'])
            for idx, item, result in \
                    zip(itertools.count(), self.testitemdetails[filepath], self.testitemstatus[filepath]):
                region = sublime.Region(point_to_offset(Point.from_lsp(item['range']['start']), view))
                annotation = '<a href="{}#idx={}&amp;version={}">Run Test</a>'.format(html.escape(uri), idx, version)
                duration = result['duration']
                if duration is not None:
                    if duration < 100:
                        annotation += " ({}ms)".format(round(duration))
                    else:
                        annotation += " ({:0.2f}s)".format(duration/1000)
                status = cast(TestItemStatus, result['status'])
                regions_by_status[status].append(region)
                if status in (TestItemStatus.Passed, TestItemStatus.Undetermined):
                    annotations[status].append(annotation)
                elif status == TestItemStatus.Pending:
                    annotations[status].append('Running…')
                elif status in (TestItemStatus.Failed, TestItemStatus.Errored):
                    annotations[status].append(annotation)
                    if idx == new_result_idx and result['message'] is not None:
                        for error_idx, message in enumerate(result['message']):
                            location = message['location']
                            if location:
                                regions_key = 'lsp_julia_testitem_error_{}_{}'.format(item['id'], error_idx)
                                view.add_regions(
                                    regions_key,
                                    [range_to_region(location['range'], view)],
                                    flags=sublime.HIDE_ON_MINIMAP | sublime.DRAW_NO_FILL | sublime.DRAW_NO_OUTLINE,
                                    annotations=["<br>".join(html.escape(message['message']).split("\n"))],
                                    annotation_color=error_annotation_color,
                                    on_close=partial(self.hide_annotation, uri, regions_key))
                                self.error_keys[filepath].add(regions_key)
                elif status == TestItemStatus.Invalid:
                    annotations[status].append('<br>'.join([
                        "The test process crashed while running this testitem.",
                        "Please check the console and consider to create an issue report in the LSP-julia GitHub repo."
                    ]))
            for testerror in self.testerrordetails[filepath]:
                region = sublime.Region(point_to_offset(Point.from_lsp(testerror['range']['start']), view))
                regions_by_status[TestItemStatus.Invalid].append(region)
                annotations[TestItemStatus.Invalid].append(testerror['error'])
            for status, regions in regions_by_status.items():
                regions_key = 'lsp_julia_testitem_{}'.format(status)
                if regions:
                    on_navigate = self.run_testitem if status not in (TestItemStatus.Pending, TestItemStatus.Invalid) \
                        else None
                    view.add_regions(
                        regions_key,
                        regions,
                        scope=TESTITEM_SCOPES[status],
                        icon=TESTITEM_ICONS[status],
                        flags=sublime.HIDE_ON_MINIMAP | sublime.DRAW_NO_FILL | sublime.DRAW_NO_OUTLINE,
                        annotations=annotations[status],
                        annotation_color=cast(str, view.style_for_scope(TESTITEM_SCOPES[status])['foreground']),
                        on_navigate=on_navigate)
                else:
                    view.erase_regions(regions_key)

    def hide_annotation(self, uri: DocumentUri, key: str) -> None:
        filepath = parse_uri(uri)[1]
        view = self.window.find_open_file(filepath)
        if view:
            view.erase_regions(key)
            self.error_keys[filepath].discard(key)

    def clear_error_annotations(self, uri: DocumentUri, testitem_id: str | None = "") -> None:
        filepath = parse_uri(uri)[1]
        view = self.window.find_open_file(filepath)
        if view:
            for key in self.error_keys.setdefault(filepath, set()).copy():
                if key.startswith('lsp_julia_testitem_error_{}'.format(testitem_id)):
                    view.erase_regions(key)
                    self.error_keys[filepath].discard(key)

    def run_testitem_request_params(
        self, uri: DocumentUri, idx: int
    ) -> TestserverRunTestitemRequestExtendedParams | None:
        filepath = parse_uri(uri)[1]
        params = self.testitemparams.get(filepath)
        if not params:
            return None
        testitem = self.testitemdetails[filepath][idx]
        code_range = testitem.get('code_range')
        if not code_range:
            return None
        code = testitem.get('code')
        if not code:
            return None
        project_path = params['project_path']
        package_path = params['package_path']
        package_name = params['package_name']
        if not any([project_path, package_path, package_name]):
            return None
        line = code_range['start']['line']
        column = code_range['start']['character']
        if column == 0:
            line -= 1  # Fix missmatch of start position between initial and subsequent reported testitem notifications
        return {
            'uri': params['uri'],
            'name': testitem['label'],
            'packageName': package_name,
            'useDefaultUsings': testitem.get('option_default_imports') is not False,
            'line': line,
            'column': column,
            'code': code,
            'project_path': project_path,
            'package_path': package_path
        }

    def run_testitem(self, href: str, focus_testitem: bool = False) -> None:
        if self.pending_result:
            self.window.status_message("Another testitem is already running")
            return
        uri, fragment = urldefrag(href)
        filepath = parse_uri(uri)[1]
        pq = parse_qs(fragment)
        idx = int(pq['idx'][0])
        version = int(pq['version'][0])
        if version != self.stored_version(uri):
            # Actually this should never happen in practice, because annotations for the corresponding view are redrawn
            # on each julia/publishTestitems notification.
            self.window.status_message("Version mismatch for testitem params")
            return
        params = self.run_testitem_request_params(uri, idx)
        if params:
            self.pending_result = True
            self.testitemstatus[filepath][idx]['status'] = TestItemStatus.Pending
            thread = threading.Thread(
                target=self.run_testitem_daemon_thread, args=(uri, idx, version, params), daemon=True)
            thread.start()
            if focus_testitem:
                view = self.window.open_file(
                    "{}:{}".format(filepath, params['line'] + 1), flags=sublime.ENCODED_POSITION)
                # In case the file wasn't open before and is still loading, add a small delay before drawing the
                # annotations.
                if view.is_loading():
                    sublime.set_timeout(partial(self.render_testitems, uri), 50)
                    sublime.set_timeout(partial(self.clear_error_annotations, uri, params['name']), 50)
                    return
            self.render_testitems(uri)
            self.clear_error_annotations(uri, params['name'])

    def run_testitem_daemon_thread(
        self, uri: DocumentUri, idx: int, version: int, params: TestserverRunTestitemRequestExtendedParams
    ) -> None:
        try:
            file_directory = os.path.dirname(parse_uri(params['uri'])[1])
            params_json = json.dumps(params, separators=(',', ':'))
            result_json = subprocess.check_output([
                LspJuliaPlugin.julia_exe(),
                "--startup-file=no",
                "--history-file=no",
                "--project={}".format(LspJuliaPlugin.testrunnerdir()),
                os.path.join(LspJuliaPlugin.testrunnerdir(), "runtestitem.jl"),
                params_json
            ], cwd=file_directory, startupinfo=startupinfo()).decode("utf-8")
            result = json.loads(result_json)
            sublime.set_timeout(partial(self.on_result, uri, idx, version, result))
        except Exception:
            self.pending_result = False
            filepath = parse_uri(uri)[1]
            self.testitemstatus[filepath][idx]['status'] = TestItemStatus.Invalid
            traceback.print_exc()
            sublime.set_timeout(partial(self.render_testitems, uri))

    def on_result(
        self, uri: DocumentUri, idx: int, version: int, params: TestserverRunTestitemRequestParamsReturn
    ) -> None:
        self.pending_result = False
        filepath = parse_uri(uri)[1]
        if self.testitemparams[filepath]['version'] == version:
            self.testitemstatus[filepath][idx] = params
            self.render_testitems(uri, idx)
        else:
            # Ignore result if the language server has notified about a new version of testitems for this file in the
            # meantime. The index of the stored testitem might have changed! Search through all testitems for an item
            # with "pending" status and reset status if found. Don't draw new error annotations.
            for idx, status in enumerate(self.testitemstatus[filepath]):
                if status['status'] == TestItemStatus.Pending:
                    self.testitemstatus[filepath][idx]['status'] = TestItemStatus.Undetermined
            self.render_testitems(uri)


class JuliaRunTestitemCommand(LspWindowCommand):

    session_name = SESSION_NAME

    def run(self) -> None:
        session = self.session()
        if not session:
            return
        plugin = cast(LspJuliaPlugin, session._plugin)  # pyright: ignore[reportPrivateUsage]
        items: List[sublime.QuickPanelItem] = []
        self.hrefs: List[str] = []
        for filepath, details in plugin.testitems.testitemdetails.items():
            for idx, testitem in enumerate(details):
                status = cast(TestItemStatus, plugin.testitems.testitemstatus[filepath][idx]['status'])
                kind = TESTITEM_KINDS.get(status, sublime.KIND_AMBIGUOUS)
                details = ", ".join(testitem.get('option_tags') or [])
                location = "{}:{}".format(filepath, testitem['range']['start']['line'] + 1)
                items.append(
                    sublime.QuickPanelItem(testitem['label'], details=details, annotation=location, kind=kind))
                uri = plugin.testitems.testitemparams[filepath]['uri']
                version = plugin.testitems.testitemparams[filepath]['version']
                self.hrefs.append("{}#idx={}&amp;version={}".format(uri, idx, version))
        session.window.show_quick_panel(items, on_select=partial(self._on_select, plugin), placeholder="Run @testitem")

    def is_enabled(self) -> bool:
        session = self.session()
        if session is None:
            return False
        plugin = cast(LspJuliaPlugin, session._plugin)  # pyright: ignore[reportPrivateUsage]
        if plugin.testitems.pending_result:
            return False
        return any(testitem for testitems in plugin.testitems.testitemdetails.values()
                   for testitem in testitems
                   if not testitem.get('error'))

    def _on_select(self, plugin: LspJuliaPlugin, idx: int) -> None:
        if idx > -1:
            href = self.hrefs[idx]
            plugin.testitems.run_testitem(href, focus_testitem=True)
