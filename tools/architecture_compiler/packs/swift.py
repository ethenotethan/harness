"""Swift language pack.

Baseline passes plus the three Portal's own map is built on (store declarations
by naming convention, SwiftUI triggers, RPC call sites), so a Swift service
compiled here sees what Portal's hand-written compiler sees at the construct
level. Portal keeps its own compiler until this pack plus a project rule table
reproduces its map.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Sequence, Tuple

from .base import Declaration, ImportRecord, LanguagePack, Rule, child_of_type, descendant_of_type, node_end_line, node_line, node_text, walk

APPLE_SDK = frozenset("""
Foundation SwiftUI UIKit AppKit Combine Swift Dispatch os OSLog CoreGraphics CoreData CloudKit AVFoundation Security WebKit CryptoKit
Network Observation Charts StoreKit UserNotifications MapKit CoreLocation Speech NaturalLanguage Vision CoreML Metal MetalKit QuartzCore
XCTest Testing PhotosUI Photos CoreImage CoreMedia CoreAudio AudioToolbox Accelerate simd GameplayKit SwiftData TipKit WidgetKit AppIntents
Intents BackgroundTasks LocalAuthentication AuthenticationServices SafariServices MessageUI ContactsUI Contacts EventKit HealthKit HomeKit
CoreBluetooth ExternalAccessory MultipeerConnectivity NetworkExtension SystemConfiguration IOKit Darwin Glibc _Concurrency
UniformTypeIdentifiers ImageIO PDFKit Quartz ScreenCaptureKit ServiceManagement Carbon ApplicationServices CoreServices CoreText CoreVideo
VideoToolbox MediaPlayer ReplayKit Symbols DeveloperToolsSupport Synchronization Distributed RegexBuilder TabularData CreateML SoundAnalysis
PencilKit ARKit RealityKit SceneKit SpriteKit ModelIO GameController GameKit WeatherKit PassKit JavaScriptCore XPC Compression zlib
CoreFoundation CoreMotion CoreHaptics CoreSpotlight CoreTelephony CoreNFC CoreWLAN CoreMIDI NotificationCenter Cocoa ObjectiveC
""".split())

STORE_NAME_RE = re.compile(r"(?:Store|Cache|Inventory|Ledger)$")
TRIGGER_RE = re.compile(r"\.(?:onAppear|onDisappear|onChange|onReceive|onSubmit|onTapGesture|task|refreshable|swipeActions)\s*[({]|\b(?:Button|Toggle|Picker)\s*\(")
TYPED_PROPERTY_RE = re.compile(r"(?:var|let)\s+(\w+)\s*:\s*(\w+)\b")
CALL_RE = re.compile(r"\b(\w+)\.(\w+)\s*\(")

DECLARATION_KEYWORDS = {"class": "class", "struct": "struct", "enum": "enum", "actor": "actor", "extension": "extension"}


def census(root: Any, source: bytes) -> List[Declaration]:
    found: List[Declaration] = []
    for node in walk(root):
        if node.type == "class_declaration":
            keyword = next((c.type for c in node.children if c.type in DECLARATION_KEYWORDS), "class")
            kind = DECLARATION_KEYWORDS[keyword]
            name_node = child_of_type(node, "type_identifier") or descendant_of_type(child_of_type(node, "user_type") or node, "type_identifier")
            if name_node is not None:
                found.append(Declaration(kind, node_text(name_node, source), node_line(node), node_end_line(node)))
        elif node.type == "protocol_declaration":
            name_node = child_of_type(node, "type_identifier")
            if name_node is not None:
                found.append(Declaration("protocol", node_text(name_node, source), node_line(node), node_end_line(node)))
        elif node.type in ("function_declaration", "protocol_function_declaration"):
            name_node = child_of_type(node, "simple_identifier")
            if name_node is not None:
                found.append(Declaration("function", node_text(name_node, source), node_line(node), node_end_line(node)))
    return found


def imports(root: Any, source: bytes) -> List[ImportRecord]:
    records: List[ImportRecord] = []
    for node in root.children:
        if node.type == "import_declaration":
            identifier = child_of_type(node, "identifier")
            if identifier is not None:
                name = node_text(identifier, source).split(".")[0]
                records.append(ImportRecord(name, node_line(node), name))
    return records


def entrypoints(root: Any, text: str, rel: str) -> List[Tuple[int, str]]:
    match = re.search(r"^\s*@main\b", text, re.MULTILINE)
    if match:
        return [(text.count("\n", 0, match.start()) + 1, "@main")]
    if rel.rsplit("/", 1)[-1] == "main.swift":
        return [(1, "main.swift")]
    return []


def local_roots(rels: Sequence[str], config: Dict[str, Any]) -> frozenset:
    roots = set(str(m) for m in config.get("local_modules") or [])
    for rel in rels:
        parts = rel.split("/")
        roots.update(parts[:-1])
        roots.add(parts[-1].rsplit(".", 1)[0])
    return frozenset(roots)


def type_kind(declaration: Declaration) -> str:
    if declaration.kind in ("class", "struct", "actor") and STORE_NAME_RE.search(declaration.name):
        return "store"
    return "type"


def extra_edges(code: str, declarations: Sequence[Declaration], rel: str) -> List[Tuple[str, str, str, str, int, str]]:
    """SwiftUI triggers: a view type whose action or lifecycle closure calls a
    method on a same-type property typed as another local type drives it."""
    edges: List[Tuple[str, str, str, str, int, str]] = []
    lines = code.split("\n")
    for declaration in declarations:
        if not declaration.is_type:
            continue
        body = "\n".join(lines[declaration.line - 1:declaration.end_line])
        properties = {m.group(1): m.group(2) for m in TYPED_PROPERTY_RE.finditer(body)}
        if not properties:
            continue
        for match in TRIGGER_RE.finditer(body):
            line = declaration.line + body.count("\n", 0, match.start())
            window = "\n".join(lines[line - 1:min(len(lines), line + 3)])
            for call in CALL_RE.finditer(window):
                target = properties.get(call.group(1))
                if target and target != declaration.name:
                    edges.append((declaration.name, target, "drives", "interplay", line, "swift.ui_trigger"))
    return edges


RULES: Tuple[Rule, ...] = (
    Rule("swift.import_external", "An import of a module outside the Apple SDK and outside this service", "boundary", "code", r"(?!x)x"),
    Rule("swift.store_declaration", "A class, struct or actor whose name ends in Store, Cache, Inventory or Ledger (Portal's store convention)", "store", "code",
         r"\b(?:class|struct|actor)\s+(\w+(?:Store|Cache|Inventory|Ledger))\b", label_group=1),
    Rule("swift.ui_trigger", "A SwiftUI action (Button, Toggle, Picker) or lifecycle hook (onAppear, task, onChange, onReceive, onSubmit, onTapGesture, refreshable, swipeActions)", "trigger", "code",
         TRIGGER_RE.pattern),
    Rule("swift.rpc_call", "A JSON-RPC call site: call(\"namespace.method\") — the namespace becomes an endpoint the caller invokes", "wiring", "raw",
         r"\bcall(?:WithRetry)?\(\s*\"([a-z][A-Za-z0-9_]*)\.[A-Za-z0-9_.]*\"", produces="endpoint", sub_kind="rpc_namespace", label_group=1,
         relation="invokes", edge_class="interplay"),
    Rule("swift.route", "A server-side route registration (Vapor style: app.get(\"/path\"))", "trigger", "raw",
         r"\b(?:app|routes|group|router)\.(?:get|post|put|delete|patch)\(\s*\"([^\"]+)\"", produces="endpoint", sub_kind="route", label_group=1),
    Rule("swift.http_client", "An HTTP client: URLSession or URLRequest", "boundary", "code", r"\bURLSession\b|\bURLRequest\b", produces="resource", sub_kind="http_client"),
    Rule("swift.socket_or_server", "A socket: URLSessionWebSocketTask, NWConnection, NWListener, NetService", "boundary", "code",
         r"\bURLSessionWebSocketTask\b|\bNWConnection\b|\bNWListener\b|\bNetService\b", produces="resource", sub_kind="socket"),
    Rule("swift.file_write", "A file or directory written: Data/String.write(to:), FileManager.createFile/createDirectory", "store", "code",
         r"\.write\(to:|\.write\(toFile:|FileManager\.default\.createFile\(|\.createDirectory\(", produces="store", sub_kind="file"),
    Rule("swift.process_spawn", "A child process: Process(), NSTask, posix_spawn", "behaviour", "code", r"\bProcess\(\)|\bNSTask\b|\bposix_spawn\b"),
    Rule("swift.env_read", "An environment read: ProcessInfo.processInfo.environment", "boundary", "code", r"ProcessInfo\.processInfo\.environment"),
    Rule("swift.persistent_store", "A persistence API: UserDefaults, @AppStorage, SecItem*, NSPersistentContainer, ModelContainer, sqlite3_open", "store", "code",
         r"\bUserDefaults\b|@AppStorage\b|\bSecItem(?:Add|CopyMatching|Update|Delete)\b|\bNSPersistentContainer\b|\bModelContainer\b|\bsqlite3_open\b",
         produces="store", sub_kind="mechanism"),
    Rule("swift.concurrency", "Concurrency: Task { }, Task.detached, DispatchQueue, withTaskGroup, async let", "behaviour", "code",
         r"\bTask\s*(?:\.detached)?\s*[({]|\bDispatchQueue\b|\bwithTaskGroup\b|\bwithThrowingTaskGroup\b|\basync\s+let\b"),
    Rule("swift.state_machine", "An enum-typed stored property assigned a case somewhere in the type: a lifecycle state, and each assignment a transition", "behaviour", "code", r"(?!x)x"),
)

PACK = LanguagePack(
    name="swift",
    label="Swift",
    extensions=(".swift",),
    grammar="swift",
    comment_types=frozenset({"comment", "multiline_comment"}),
    string_types=frozenset({"line_string_literal", "multi_line_string_literal", "raw_string_literal", "regex_literal"}),
    stdlib=APPLE_SDK,
    rules=RULES,
    census=census,
    imports=imports,
    entrypoints=entrypoints,
    local_roots=local_roots,
    type_kind=type_kind,
    enum_separator=".",
    typed_property_pattern=TYPED_PROPERTY_RE.pattern,
    extra_edges=extra_edges,
    limitations=(
        "Swift: a SwiftUI trigger is attributed when the closure calls a method on a property declared in the same type body with an explicit type; inferred types and environment objects are not followed.",
        "Swift: result builders, property wrappers and macros are seen as the source text, not as what they expand to.",
    ),
)
