// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright 2026 RoyÅke (Roy-Åke Martinsson)
//
// Feedback: three screens. 1 how it was, 2 what happened (typed on the panel's own keyboard), 3 what will be sent,
// with a name and the Steam ID both left to the person. Nothing is sent before Send. What cannot be sent is handed
// to the launcher as one DOSPLEX-OUTBOX line, which keeps it for the next time. The rules are in logic.mjs.
import QtQuick
import "logic.mjs" as Logic

Item {
    id: form
    property real u: 1
    property var context: ({})
    property string endpoint: ""
    property bool trace: false               // a test run asked for the keys to be printed; never set otherwise

    property int step: 1
    property int moodAt: 0
    property string text: ""
    property string name: ""
    property bool steamIdOn: false
    property bool typingName: false          // step 3: the keyboard is open for the name
    property int row: 2                      // step 3: 0 name, 1 Steam ID, 2 the buttons
    property int button: 0                   // step 3: 0 Send, 1 Cancel
    property string note: ""                 // a short line under the heading when something is missing
    property string result: ""               // "", "sending", "sent", "kept"
    property string reference: ""
    readonly property string mood: Logic.MOODS[moodAt].id
    readonly property bool hasSteamId: String(context.steam_id || "").length === 17
    readonly property var payload: Logic.buildPayload({ mood: mood, text: text, name: name, steamIdOn: steamIdOn }, context)

    function state() { return { step: step, mood: mood, text: text, name: name, steamIdOn: steamIdOn }; }

    function key(k) {
        note = "";
        if (result === "sending") return;
        if (result !== "") { Qt.quit(); return; }
        if (step === 1) keyMood(k);
        else if (step === 2) keyText(k);
        else if (typingName) keyName(k);
        else keySend(k);
        // Only in a test run: this line holds what was typed, and whatever a program prints can end in a log.
        if (trace) console.log("DOSPLEX-KEY " + k + " -> step " + step + " mood " + mood + " text [" + text + "] name ["
                               + name + "] id " + steamIdOn + " row " + row + " button " + button + " result " + result);
    }

    function keyMood(k) {
        if (k === "left") moodAt = (moodAt + 2) % 3;
        else if (k === "right") moodAt = (moodAt + 1) % 3;
        else if (k === "return") step = 2;
        else if (k === "escape") Qt.quit();
    }

    function keyText(k) {
        if (k === "escape") { step = 1; return; }
        if (k.indexOf("char:") === 0) { text = Logic.typeChar({ text: text, maxLength: 600 }, k.substring(5)).text; return; }
        if (k === "backspace") { text = Logic.backspace({ text: text, maxLength: 600 }).text; return; }
        if (k !== "return") { keys.move(k); return; }
        var next = keys.press({ text: text, maxLength: 600 });
        text = next.text;
        if (!next.done) return;
        if (Logic.canContinue(state())) { step = 3; row = 2; button = 0; }
        else note = "Write a few words about what happened first.";
    }

    function keyName(k) {
        if (k === "escape") { typingName = false; return; }
        if (k.indexOf("char:") === 0) { name = Logic.typeChar({ text: name, maxLength: 40 }, k.substring(5)).text; return; }
        if (k === "backspace") { name = Logic.backspace({ text: name, maxLength: 40 }).text; return; }
        if (k !== "return") { keys.move(k); return; }
        var next = keys.press({ text: name, maxLength: 40 });
        name = next.text;
        if (next.done) typingName = false;
    }

    function keySend(k) {
        if (k === "escape") { step = 2; return; }
        if (k === "up") row = Math.max(0, row - 1);
        else if (k === "down") row = Math.min(2, row + 1);
        else if ((k === "left" || k === "right") && row === 2) button = 1 - button;
        else if (k === "return" && row === 0) { typingName = true; keys.row = 1; keys.col = 0; }
        else if (k === "return" && row === 1) {
            if (hasSteamId) steamIdOn = !steamIdOn;
            else note = "No Steam ID was found for this account.";
        }
        else if (k === "return" && row === 2 && button === 1) Qt.quit();
        else if (k === "return" && row === 2) send();
    }

    function send() {
        result = "sending";
        var body = JSON.stringify(payload);
        var request = new XMLHttpRequest();
        request.onreadystatechange = function() {
            if (request.readyState !== XMLHttpRequest.DONE) return;
            giveUp.stop();
            if (request.status >= 200 && request.status < 300) {
                try { reference = String(JSON.parse(request.responseText).reference || ""); } catch (e) { reference = ""; }
                result = "sent";
            } else {
                keep();
            }
            leave.start();
        };
        request.open("POST", endpoint, true);
        request.setRequestHeader("Content-Type", "application/json");
        request.send(body);
        giveUp.start();
    }

    // Not sent: the launcher keeps it and sends it the next time the form is opened.
    function keep() {
        if (result !== "sending") return;
        console.log("DOSPLEX-OUTBOX " + Logic.outboxEntry(payload, new Date().toISOString()));
        result = "kept";
    }

    Timer { id: giveUp; interval: 10000; onTriggered: { form.keep(); leave.start(); } }
    Timer { id: leave; interval: 8000; onTriggered: Qt.quit() }

    // --- what is drawn --------------------------------------------------------------------------------------
    Text {
        id: kicker
        x: 40 * u; y: 28 * u
        text: "Feedback  ·  " + (result !== "" ? "done" : "step " + step + " of 3")
        color: "#f8c21c"; font.pixelSize: 22 * u; font.bold: true
    }
    Text {
        id: heading
        x: 40 * u; y: 62 * u
        width: parent.width - 80 * u
        wrapMode: Text.WordWrap
        text: result === "sending" ? "Sending…"
            : result === "sent" ? "Thank you."
            : result === "kept" ? "Thank you. It is saved."
            : step === 1 ? "How was it?" : step === 2 ? "What happened?"
            : typingName ? "Sign it, if you like" : "Send your feedback"
        color: "#ffffff"; font.pixelSize: 40 * u; font.bold: true
    }
    Text {
        id: under
        x: 40 * u; y: heading.y + heading.height + 6 * u
        width: parent.width - 80 * u
        wrapMode: Text.WordWrap
        text: note !== "" ? note
            : result === "sent" ? (reference !== "" ? "Your reference is " + reference + ". Mention it if you write to us about this."
                                                    : "Your feedback arrived.")
            : result === "kept" ? "This machine could not reach us just now. Your feedback is sent the next time you open this form."
            : result === "sending" ? ""
            : step === 1 ? "Left and right to choose, A to go on, B to close."
            : step === 2 ? (mood === "good" ? "Tell us what you liked, or choose Done to skip. B goes back."
                                            : "A few words are enough. Choose Done when you are ready. B goes back.")
            : typingName ? "Any name you like, or none. Choose Done when you are ready."
            : "This is everything that is sent. Up and down to move, A to choose, B to go back."
        color: note !== "" ? "#ffb454" : "#b8c0cc"; font.pixelSize: 22 * u
    }

    // Step 1: three choices.
    Row {
        visible: step === 1 && result === ""
        anchors.horizontalCenter: parent.horizontalCenter
        y: under.y + under.height + 70 * u
        spacing: 20 * u
        Repeater {
            model: Logic.MOODS
            Rectangle {
                required property var modelData
                required property int index
                width: Math.min(270 * u, (form.width - 120 * u) / 3); height: 150 * u; radius: 14 * u
                color: index === form.moodAt ? "#f8c21c" : "#1c2633"
                Text {
                    anchors.centerIn: parent
                    text: parent.modelData.label
                    color: parent.index === form.moodAt ? "#101820" : "#ffffff"
                    font.pixelSize: 28 * u; font.bold: true
                }
            }
        }
    }

    // Step 2, and the name of step 3: what is typed, and the keyboard.
    Rectangle {
        id: field
        visible: (step === 2 || typingName) && result === ""
        x: 40 * u; y: under.y + under.height + 24 * u
        width: parent.width - 80 * u
        height: Math.max(70 * u, keys.y - y - 34 * u)
        radius: 12 * u; color: "#1c2633"; clip: true
        Text {
            anchors.fill: parent; anchors.margins: 16 * u
            wrapMode: Text.Wrap
            verticalAlignment: Text.AlignBottom
            text: (form.typingName ? form.name : form.text) + "▏"
            color: "#ffffff"; font.pixelSize: 26 * u
        }
    }
    Text {
        visible: field.visible
        anchors.right: field.right; y: field.y + field.height + 4 * u
        text: form.typingName ? form.name.length + " of 40" : form.text.length + " of 600"
        color: "#b8c0cc"; font.pixelSize: 18 * u
    }
    KeyGrid {
        id: keys
        u: form.u
        visible: field.visible
        x: 20 * u; width: parent.width - 40 * u
        anchors.bottom: parent.bottom; anchors.bottomMargin: 20 * u
    }

    // Step 3: what is sent, the two things left to the person, and the buttons.
    Column {
        visible: step === 3 && !typingName && result === ""
        x: 40 * u; y: under.y + under.height + 22 * u
        width: parent.width - 80 * u
        spacing: 12 * u
        Rectangle {
            width: parent.width; height: sent.height + 28 * u; radius: 12 * u; color: "#17202b"
            Column {
                id: sent
                x: 18 * u; y: 14 * u; width: parent.width - 36 * u; spacing: 4 * u
                Repeater {
                    model: Logic.summaryLines(form.payload, Logic.MOODS[form.moodAt].label)
                    Text {
                        required property string modelData
                        width: sent.width; wrapMode: Text.Wrap; maximumLineCount: 3; elide: Text.ElideRight
                        text: modelData; color: "#dfe5ee"; font.pixelSize: 20 * u
                    }
                }
            }
        }
        Rectangle {
            width: parent.width; height: 62 * u; radius: 12 * u
            color: form.row === 0 ? "#f8c21c" : "#1c2633"
            Text {
                x: 18 * u; anchors.verticalCenter: parent.verticalCenter
                text: "Sign it, if you like:  " + (form.name !== "" ? form.name : "no name")
                color: form.row === 0 ? "#101820" : "#ffffff"; font.pixelSize: 24 * u
            }
        }
        Rectangle {
            width: parent.width; height: 104 * u; radius: 12 * u
            color: form.row === 1 ? "#f8c21c" : "#1c2633"
            Column {
                x: 18 * u; anchors.verticalCenter: parent.verticalCenter; spacing: 2 * u
                Text {
                    text: "Include my Steam ID:  " + (form.steamIdOn ? "yes" : "no")
                    color: form.row === 1 ? "#101820" : "#ffffff"; font.pixelSize: 24 * u
                }
                Text {
                    text: form.hasSteamId ? form.context.steam_id + "  ·  so that we know it is you if you write again, and can say thanks. It is never shown to anyone else."
                                          : "No Steam ID was found for this account."
                    width: form.width - 116 * u; wrapMode: Text.WordWrap; maximumLineCount: 2; elide: Text.ElideRight
                    color: form.row === 1 ? "#2a2410" : "#b8c0cc"; font.pixelSize: 16 * u
                }
            }
        }
        Text {
            width: parent.width; wrapMode: Text.WordWrap
            text: "Thank you. Feedback like this is what makes Dosplex better, and it is greatly appreciated."
            color: "#b8c0cc"; font.pixelSize: 20 * u
        }
        Row {
            spacing: 16 * u
            Repeater {
                model: ["Send", "Cancel"]
                Rectangle {
                    required property string modelData
                    required property int index
                    readonly property bool on: form.row === 2 && form.button === index
                    width: 220 * u; height: 66 * u; radius: 12 * u
                    color: on ? "#f8c21c" : "#1c2633"
                    Text {
                        anchors.centerIn: parent; text: parent.modelData
                        color: parent.on ? "#101820" : "#ffffff"; font.pixelSize: 26 * u; font.bold: true
                    }
                }
            }
        }
    }
}
