// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright 2026 RoyÅke (Roy-Åke Martinsson)
//
// The panel's own keyboard: the keys of Logic.KEY_ROWS, one of them under the cursor. The page that uses it
// moves the cursor (move) and presses the key under it (press); what a key does is decided in logic.mjs.
import QtQuick
import "logic.mjs" as Logic

Item {
    id: grid
    property real u: 1
    property int row: 1
    property int col: 0
    property bool shift: false
    readonly property var names: ({ "shift": "Shift", "space": "Space", "backspace": "Delete", "done": "Done" })
    readonly property real cell: Math.min(width / 13, 62 * u)
    height: Logic.KEY_ROWS.length * (cell + 6 * u)

    function move(direction) {
        var next = Logic.moveKey({ row: row, col: col }, direction);
        row = next.row; col = next.col;
    }

    // Returns the new editing state {text, shift, done} for the key under the cursor.
    function press(state) {
        var next = Logic.pressKey({ text: state.text, shift: shift, maxLength: state.maxLength }, { row: row, col: col });
        shift = next.shift;
        return next;
    }

    Column {
        anchors.horizontalCenter: parent.horizontalCenter
        spacing: 6 * u
        Repeater {
            model: Logic.KEY_ROWS
            Row {
                id: line
                required property var modelData
                required property int index
                anchors.horizontalCenter: parent.horizontalCenter
                spacing: 6 * u
                Repeater {
                    model: line.modelData
                    Rectangle {
                        required property var modelData
                        required property int index
                        readonly property bool under: line.index === grid.row && index === grid.col
                        readonly property bool wide: modelData.action !== undefined
                        width: wide ? grid.cell * 3 : grid.cell
                        height: grid.cell
                        radius: 8 * u
                        color: under ? "#f8c21c" : (wide && modelData.action === "shift" && grid.shift) ? "#3d5066" : "#1c2633"
                        Text {
                            anchors.centerIn: parent
                            text: parent.wide ? grid.names[parent.modelData.action]
                                : (grid.shift ? parent.modelData.label.toUpperCase() : parent.modelData.label)
                            color: parent.under ? "#101820" : "#ffffff"
                            font.pixelSize: (parent.wide ? 22 : 28) * u
                            font.bold: parent.under
                        }
                    }
                }
            }
        }
    }
}
