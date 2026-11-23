// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright 2026 RoyÅke (Roy-Åke Martinsson)
//
// Help: one chapter of the manual at a time. Up and down read, left and right change the chapter, escape closes.
// The chapters are the files the manual builder writes (NN-name.html and index.json) in manualDir.
import QtQuick
import "logic.mjs" as Logic

Item {
    id: help
    property real u: 1
    property string page: ""
    property string manualDir: ""
    property bool trace: false
    // [{file, chapter, title}], from index.json; until it is read, the one chapter the menu page asked for.
    property var chapters: [{ "file": Logic.chapterForPage(page) + ".html", "chapter": Logic.chapterForPage(page), "title": "" }]
    property int at: 0
    property string body: ""
    property bool loaded: false

    function read(name, then) {
        var request = new XMLHttpRequest();
        request.onreadystatechange = function() {
            if (request.readyState !== XMLHttpRequest.DONE) return;
            then(request.status === 200 || request.status === 0 ? (request.responseText || "") : "");
        };
        request.open("GET", "file://" + manualDir + "/" + name, true);
        request.send();
    }

    function show(index) {
        at = index;
        loaded = false;
        flick.contentY = 0;
        read(chapters[at].file, function(text) { body = Logic.fitImages(text, flick.width * 0.94); loaded = true; });
    }

    Component.onCompleted: {
        if (manualDir === "") { loaded = true; return; }
        read("index.json", function(text) {
            var wanted = Logic.chapterForPage(page), list = [], start = 0;
            try { list = JSON.parse(text); } catch (e) { list = []; }
            if (list.length > 0) {
                for (var i = 0; i < list.length; i++)
                    if (list[i].chapter === wanted) start = i;
                chapters = list;
            }
            show(start);
        });
    }

    function key(name) {
        if (name === "escape") { Qt.quit(); return; }
        var next = Logic.helpScroll({ contentY: flick.contentY, contentHeight: flick.contentHeight, height: flick.height,
                                      chapterIndex: at, chapterCount: chapters.length }, name);
        if (trace) console.log("DOSPLEX-KEY " + name + " -> chapter " + chapters[next.chapterIndex].chapter + " y " + Math.round(next.contentY));
        if (next.chapterChanged) show(next.chapterIndex);
        else flick.contentY = next.contentY;
    }

    Text {
        id: heading
        x: 40 * u; y: 28 * u
        width: parent.width - 80 * u - where.width
        text: "Help"
        color: "#f8c21c"; font.pixelSize: 22 * u; font.bold: true
    }
    Text {
        id: where
        anchors.right: parent.right; anchors.rightMargin: 40 * u
        y: 28 * u
        text: (at + 1) + " of " + chapters.length
        color: "#b8c0cc"; font.pixelSize: 22 * u
    }
    Rectangle { x: 40 * u; y: 66 * u; width: parent.width - 80 * u; height: Math.max(1, 2 * u); color: "#2a3644" }

    Flickable {
        id: flick
        x: 40 * u; y: 84 * u
        width: parent.width - 80 * u
        height: parent.height - 84 * u - 64 * u
        contentWidth: width
        contentHeight: text.height
        clip: true
        Behavior on contentY { NumberAnimation { duration: 120 } }
        Text {
            id: text
            width: flick.width
            text: !loaded ? "" : body !== "" ? body : "<p>This page of the manual is not installed.</p>"
            textFormat: Text.RichText
            baseUrl: manualDir === "" ? "" : "file://" + manualDir + "/"   // where a chapter's pictures are
            wrapMode: Text.WordWrap
            color: "#ffffff"; font.pixelSize: 24 * u
            lineHeight: 1.15
        }
    }

    Text {
        x: 40 * u
        anchors.bottom: parent.bottom; anchors.bottomMargin: 22 * u
        text: "Up and down to read  ·  Left and right for the chapter before and after  ·  B to close"
        color: "#b8c0cc"; font.pixelSize: 18 * u
    }
}
