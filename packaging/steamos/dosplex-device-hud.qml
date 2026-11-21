// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright 2026 RoyÅke (Roy-Åke Martinsson)
import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15
import QtQuick.Window 2.15

Window {
    id: root
    width: 640
    height: 800
    visible: true
    color: "#0d0f10"
    title: "dosplex-hud"

    property string selectedDeviceId: ""
    property string selectedDeviceType: ""
    property string selectedSide: ""

    property string leftMode: "gaming"
    property string rightMode: "gaming"
    property string leftRole: "Main"
    property string rightRole: "Guest"

    ListModel { id: deviceModel }

    Rectangle {
        anchors.fill: parent
        color: "#0d0f10"

        ColumnLayout {
            anchors.fill: parent
            spacing: 0

            Rectangle {
                Layout.fillWidth: true
                Layout.preferredHeight: 54
                color: "#151718"
                RowLayout {
                    anchors.fill: parent
                    anchors.leftMargin: 16
                    anchors.rightMargin: 12
                    Label {
                        text: "Device routing"
                        color: "#eef2f0"
                        font.pixelSize: 16
                        font.weight: Font.DemiBold
                    }
                    Item { Layout.fillWidth: true }
                    Label {
                        text: selectedDeviceId === "" ? "Select device" : "Choose destination"
                        color: selectedDeviceId === "" ? "#7a8380" : "#88d8a2"
                        font.pixelSize: 12
                    }
                    Button {
                        id: closeBtn
                        Layout.preferredWidth: 34
                        Layout.preferredHeight: 34
                        contentItem: Label {
                            text: "X"
                            color: closeBtn.hovered ? "#ffffff" : "#8d9692"
                            horizontalAlignment: Text.AlignHCenter
                            verticalAlignment: Text.AlignVCenter
                        }
                        background: Rectangle {
                            color: closeBtn.hovered ? "#242829" : "transparent"
                            radius: 6
                        }
                        onClicked: backend ? backend.quit() : Qt.quit()
                    }
                }
            }

            RowLayout {
                Layout.fillWidth: true
                Layout.fillHeight: true
                spacing: 0

                SidePanel {
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    side: "left"
                    title: leftRole
                    subtitle: "Left side"
                    accent: "#48c774"
                    currentMode: leftMode
                }

                Rectangle { Layout.preferredWidth: 1; Layout.fillHeight: true; color: "#272d2b" }

                SidePanel {
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    side: "right"
                    title: rightRole
                    subtitle: "Right side"
                    accent: "#f08a5d"
                    currentMode: rightMode
                }
            }

            FreePanel {
                Layout.fillWidth: true
                Layout.preferredHeight: 170
            }
        }
    }

    component SidePanel: Rectangle {
        property string side
        property string title
        property string subtitle
        property color accent
        property string currentMode
        color: "#101314"

        DropArea {
            anchors.fill: parent
            onEntered: dropOverlay.opacity = 0.16
            onExited: dropOverlay.opacity = 0
            onDropped: function(drag) {
                dropOverlay.opacity = 0
                root.assignTo(side, drag.source.cardDeviceId, drag.source.cardDeviceType)
            }
        }

        MouseArea {
            anchors.fill: parent
            acceptedButtons: Qt.LeftButton
            onClicked: root.assignSelectedTo(side)
        }

        Rectangle {
            id: dropOverlay
            anchors.fill: parent
            color: accent
            opacity: 0
            Behavior on opacity { NumberAnimation { duration: 110 } }
        }

        ColumnLayout {
            anchors.fill: parent
            spacing: 0

            Rectangle {
                Layout.fillWidth: true
                Layout.preferredHeight: 62
                color: "transparent"
                Column {
                    anchors.left: parent.left
                    anchors.leftMargin: 18
                    anchors.verticalCenter: parent.verticalCenter
                    Label { text: title; color: accent; font.pixelSize: 13; font.weight: Font.Bold }
                    Label { text: subtitle; color: "#68716d"; font.pixelSize: 11 }
                }
            }

            Rectangle {
                Layout.fillWidth: true
                Layout.preferredHeight: 48
                color: "#161a1b"
                RowLayout {
                    anchors.fill: parent
                    anchors.margins: 8
                    spacing: 6
                    
                    ModeButton {
                        text: "Gaming"
                        active: currentMode === "gaming"
                        onClicked: root.setMode(side, "gaming")
                        Layout.fillWidth: true
                    }
                    ModeButton {
                        text: "Desktop"
                        active: currentMode === "desktop"
                        onClicked: root.setMode(side, "desktop")
                        Layout.fillWidth: true
                    }
                }
            }

            Flickable {
                Layout.fillWidth: true
                Layout.fillHeight: true
                contentHeight: sideColumn.height
                clip: true
                ColumnLayout {
                    id: sideColumn
                    width: parent.width
                    spacing: 2
                    Repeater {
                        model: deviceModel
                        delegate: DeviceCard {
                            visible: model.side === side
                            width: sideColumn.width
                            height: visible ? 58 : 0
                            sideAccent: accent
                        }
                    }
                }
            }
        }
    }

    component ModeButton: Button {
        id: modeBtn
        property bool active: false
        contentItem: Label {
            text: modeBtn.text
            color: active ? "#ffffff" : "#8d9692"
            font.pixelSize: 11
            font.weight: active ? Font.Bold : Font.Normal
            horizontalAlignment: Text.AlignHCenter
            verticalAlignment: Text.AlignVCenter
        }
        background: Rectangle {
            color: active ? "#2d3536" : (modeBtn.hovered ? "#1d2223" : "transparent")
            radius: 4
            border.color: active ? "#4d5556" : "transparent"
            border.width: 1
        }
    }

    component FreePanel: Rectangle {
        color: "#141819"
        border.color: "#252c2a"
        border.width: 1

        DropArea {
            anchors.fill: parent
            onEntered: freeOverlay.opacity = 0.14
            onExited: freeOverlay.opacity = 0
            onDropped: function(drag) {
                freeOverlay.opacity = 0
                root.assignTo("follow", drag.source.cardDeviceId, drag.source.cardDeviceType)
            }
        }

        MouseArea {
            anchors.fill: parent
            acceptedButtons: Qt.LeftButton
            onClicked: root.assignSelectedTo("follow")
        }

        Rectangle {
            id: freeOverlay
            anchors.fill: parent
            color: "#8d9692"
            opacity: 0
            Behavior on opacity { NumberAnimation { duration: 110 } }
        }

        ColumnLayout {
            anchors.fill: parent
            anchors.margins: 12
            spacing: 8
            RowLayout {
                Layout.fillWidth: true
                Label { text: "Follow"; color: "#c8cfcc"; font.pixelSize: 13; font.weight: Font.DemiBold }
                Item { Layout.fillWidth: true }
                Label { text: "Latest touched side gets selected input"; color: "#68716d"; font.pixelSize: 11 }
            }
            Flickable {
                Layout.fillWidth: true
                Layout.fillHeight: true
                contentWidth: freeRow.width
                clip: true
                RowLayout {
                    id: freeRow
                    height: parent.height
                    spacing: 6
                    Repeater {
                        model: deviceModel
                        delegate: DeviceCard {
                            visible: model.side === "follow" || model.side === "free"
                            width: visible ? 196 : 0
                            height: 58
                            sideAccent: "#8d9692"
                        }
                    }
                }
            }
        }
    }

    component DeviceCard: Item {
        property color sideAccent
        property string cardDeviceId: model.deviceId
        property string cardDeviceType: model.deviceType
        property bool selected: root.selectedDeviceId === model.deviceId && root.selectedDeviceType === model.deviceType

        Rectangle {
            id: card
            anchors.fill: parent
            anchors.margins: 5
            radius: 7
            color: selected ? "#20302a" : (mouseArea.containsMouse ? "#1a1f20" : "#15191a")
            border.color: selected || Drag.active ? sideAccent : "#262c2d"
            border.width: selected || Drag.active ? 2 : 1

            property string cardDeviceId: model.deviceId
            property string cardDeviceType: model.deviceType

            Drag.active: mouseArea.drag.active
            Drag.hotSpot.x: width / 2
            Drag.hotSpot.y: height / 2

            MouseArea {
                id: mouseArea
                anchors.fill: parent
                hoverEnabled: true
                drag.target: card
                onClicked: root.selectDevice(model.deviceId, model.deviceType, model.side)
                onReleased: {
                    card.Drag.drop()
                    card.x = 0
                    card.y = 0
                }
            }

            RowLayout {
                anchors.fill: parent
                anchors.leftMargin: 10
                anchors.rightMargin: 10
                spacing: 9
                Rectangle {
                    Layout.preferredWidth: 34
                    Layout.preferredHeight: 34
                    radius: 6
                    color: "#202626"
                    border.color: "#303838"
                    Label {
                        anchors.centerIn: parent
                        text: root.iconText(model.icon, model.deviceType)
                        color: "#dce3df"
                        font.pixelSize: 10
                        font.weight: Font.Bold
                    }
                }
                ColumnLayout {
                    Layout.fillWidth: true
                    spacing: 1
                    Label {
                        Layout.fillWidth: true
                        text: model.name
                        color: "#edf2ef"
                        font.pixelSize: 13
                        elide: Text.ElideRight
                    }
                    Label {
                        text: model.deviceType === "audio" ? "Audio output" : "Input device"
                        color: "#78827e"
                        font.pixelSize: 10
                    }
                }
            }
        }
    }

    function iconText(icon, deviceType) {
        if (icon === "mouse") return "MSE"
        if (icon === "gamepad") return "PAD"
        if (icon === "touch") return "TCH"
        if (icon === "headphones") return "HP"
        if (icon === "display") return "TV"
        if (deviceType === "audio") return "AUD"
        return "KBD"
    }

    function selectDevice(deviceId, deviceType, side) {
        selectedDeviceId = deviceId
        selectedDeviceType = deviceType
        selectedSide = side
    }

    function assignSelectedTo(side) {
        if (selectedDeviceId === "") return
        assignTo(side, selectedDeviceId, selectedDeviceType)
    }

    function assignTo(side, deviceId, deviceType) {
        if (deviceId === "") return
        for (var i = 0; i < deviceModel.count; i++) {
            if (deviceModel.get(i).deviceId === deviceId &&
                    deviceModel.get(i).deviceType === deviceType) {
                deviceModel.setProperty(i, "side", side)
                break
            }
        }
        selectedDeviceId = ""
        selectedDeviceType = ""
        selectedSide = ""
        if (typeof backend !== "undefined") backend.assignDevice(deviceId, deviceType, side)
    }

    function initDevices(list) {
        deviceModel.clear()
        for (var i = 0; i < list.length; i++) deviceModel.append(list[i])
    }

    function initModes(left, right) {
        leftMode = left
        rightMode = right
    }

    function initRoles(left, right) {
        leftRole = left
        rightRole = right
    }

    function setMode(side, mode) {
        if (side === "left") leftMode = mode
        if (side === "right") rightMode = mode
        if (typeof backend !== "undefined") backend.setMode(side, mode)
    }
}
