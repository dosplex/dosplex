// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright 2026 RoyÅke (Roy-Åke Martinsson)
import QtQuick 2.15
import QtQuick.Window 2.15

Window {
    width: 640
    height: 800
    visible: true
    color: "#5b2e14"
    title: "Dosplex Right"

    property string typedText: ""
    property string pointerText: "Touch or click to test input"

    Rectangle {
        anchors.fill: parent
        gradient: Gradient {
            GradientStop { position: 0.0; color: "#92502a" }
            GradientStop { position: 1.0; color: "#34180d" }
        }

        focus: true
        Keys.onPressed: function(event) {
            if (event.key === Qt.Key_Backspace) {
                typedText = typedText.length > 0 ? typedText.slice(0, -1) : ""
                event.accepted = true
                return
            }
            if (event.text && event.text.length > 0) {
                typedText += event.text
                event.accepted = true
            }
        }

        MouseArea {
            anchors.fill: parent
            onPressed: function(mouse) {
                parent.forceActiveFocus()
                pointerText = "Pointer: " + Math.round(mouse.x) + ", " + Math.round(mouse.y)
            }
        }

        Column {
            anchors.centerIn: parent
            spacing: 18

            Text {
                text: "GUEST SIDE"
                color: "white"
                font.pixelSize: 42
                font.bold: true
                horizontalAlignment: Text.AlignHCenter
                width: 520
            }

            Text {
                text: pointerText
                color: "#ffe8d9"
                font.pixelSize: 24
                horizontalAlignment: Text.AlignHCenter
                width: 520
                wrapMode: Text.Wrap
            }

            Rectangle {
                width: 520
                height: 80
                radius: 16
                color: "#fff7f1"
                border.color: "#d7b198"
                border.width: 2

                Text {
                    anchors.fill: parent
                    anchors.margins: 18
                    verticalAlignment: Text.AlignVCenter
                    text: typedText.length > 0 ? typedText : "Type here"
                    color: typedText.length > 0 ? "#3b1e10" : "#8a6758"
                    font.pixelSize: 28
                    elide: Text.ElideRight
                }
            }
        }
    }
}
