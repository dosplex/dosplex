// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright 2026 RoyÅke (Roy-Åke Martinsson)
import QtQuick 2.15
import QtQuick.Window 2.15

Window {
    width: 640
    height: 800
    visible: true
    visibility: Window.FullScreen
    color: "#cc1111"
    title: "DosPlex Red Test"

    Rectangle {
        anchors.fill: parent
        color: "#cc1111"

        Text {
            anchors.centerIn: parent
            text: "LEFT RED TEST"
            color: "white"
            font.pixelSize: 48
            font.bold: true
        }
    }
}
