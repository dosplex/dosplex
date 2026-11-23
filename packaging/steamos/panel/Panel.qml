// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright 2026 RoyÅke (Roy-Åke Martinsson)
//
// The panel's window. It reads its job from the command line (--help PAGE or --feedback, with --context JSON,
// --manual-dir DIR, --endpoint URL), shows Help.qml or Feedback.qml, and passes every key to that page. It ends
// when the page asks for it or after ten minutes without a key. Test hooks: --keys a,b,c presses keys by name,
// --grab FILE saves a picture of the window and ends. The rules are in logic.mjs.
import QtQuick

Window {
    id: window
    visible: true
    title: "DosPlex"
    width: 960
    height: 1080
    color: "#101820"

    property string mode: ""
    property string helpPage: ""
    property var context: ({})
    property string manualDir: ""
    property string endpoint: ""
    property string grabFile: ""
    property var keysToPress: []
    // One unit of size: the page is drawn for a side of 960 by 800 and scales with the side it is given.
    property real u: Math.max(0.5, Math.min(width / 960, height / 800))

    function argument(flag) {
        var a = Qt.application.arguments;
        for (var i = 0; i < a.length - 1; i++)
            if (String(a[i]) === flag) return String(a[i + 1]);
        return "";
    }

    function has(flag) {
        var a = Qt.application.arguments;
        for (var i = 0; i < a.length; i++)
            if (String(a[i]) === flag) return true;
        return false;
    }

    // The one place every key goes through: "up", "down", "left", "right", "return", "escape".
    function handleKey(name) {
        idle.restart();
        if (pages.item) pages.item.key(name);
    }

    Component.onCompleted: {
        mode = has("--feedback") ? "feedback" : "help";
        helpPage = mode === "help" ? argument("--help") : "";
        try { context = JSON.parse(argument("--context") || "{}"); } catch (e) { context = {}; }
        manualDir = argument("--manual-dir").replace(/\/+$/, "");
        endpoint = argument("--endpoint");
        grabFile = argument("--grab");
        var keys = argument("--keys");
        keysToPress = keys === "" ? [] : keys.split(",");
        // The page gets what it needs before it is built, so its first picture is already the right one.
        if (mode === "feedback")
            pages.setSource("Feedback.qml", { "context": context, "endpoint": endpoint, "trace": keys !== "" });
        else
            pages.setSource("Help.qml", { "page": helpPage, "manualDir": manualDir, "trace": keys !== "" });
        idle.restart();
        if (keysToPress.length > 0) pressNext.start();
        else if (grabFile !== "") grab.start();
    }

    Rectangle { anchors.fill: parent; color: "#101820" }

    Loader {
        id: pages
        anchors.fill: parent
        focus: true
        onLoaded: { item.u = Qt.binding(function() { return window.u; }); }
        Keys.onPressed: function(event) {
            var name = event.key === Qt.Key_Up ? "up" : event.key === Qt.Key_Down ? "down"
                     : event.key === Qt.Key_Left ? "left" : event.key === Qt.Key_Right ? "right"
                     : (event.key === Qt.Key_Return || event.key === Qt.Key_Enter) ? "return"
                     : event.key === Qt.Key_Escape ? "escape"
                     : event.key === Qt.Key_Backspace ? "backspace" : "";
            if (name === "") {
                // a character key, with no modifier held: the page types it as it is
                var mods = event.modifiers;
                if (event.text.length === 1 && event.text.charCodeAt(0) >= 32 && event.text.charCodeAt(0) !== 127
                        && (mods & Qt.ControlModifier) === 0 && (mods & Qt.AltModifier) === 0
                        && (mods & Qt.MetaModifier) === 0) {
                    name = "char:" + event.text;
                }
            }
            if (name === "") return;
            event.accepted = true;
            window.handleKey(name);
        }
    }

    Timer { id: idle; interval: 10 * 60 * 1000; onTriggered: Qt.quit() }

    // Test hook: the keys of --keys, one every 250 ms, starting half a second after the page is there.
    property int pressed: 0
    Timer {
        id: pressNext
        interval: pressed === 0 ? 500 : 250
        repeat: true
        onTriggered: {
            if (pressed >= keysToPress.length) {
                stop();
                if (grabFile !== "") grab.start();
                return;
            }
            window.handleKey(keysToPress[pressed]);
            pressed = pressed + 1;
        }
    }

    // Test hook: half a second after the last key, save a picture of the whole window and end.
    Timer {
        id: grab
        interval: 500
        onTriggered: {
            window.contentItem.grabToImage(function(result) {
                console.log(result.saveToFile(grabFile) ? "DOSPLEX-GRAB saved" : "DOSPLEX-GRAB failed");
                Qt.quit();
            });
        }
    }
}
