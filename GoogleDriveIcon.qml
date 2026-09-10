import QtQuick
import QtQuick.Shapes
import qs.Commons

// The Google Drive mark drawn to Google's real 2020 geometry, but wearing the
// Omarchy theme instead of Google's palette.
//
// The mark is three flat faces meeting at folds. Flattening it to one colour
// would collapse it into a plain triangle, so each region keeps its relative
// tone: the brightest face is the theme foreground and every other region
// steps down from it, in the same order the brand colours do by luminance
// (#ffba00 > #00ac47 > #2684fc > #ea4335 > #00832d > #0066da).
//
// Only darker steps are used, never lighter. `Qt.lighter` on an already-light
// foreground clips to white on dark themes, and the same trick inverts badly
// on light ones; stepping down from the foreground reads correctly on both.
Item {
  id: root

  property real iconSize: Style.font.icon
  property color color: Color.foreground

  readonly property real viewWidth: 87.3
  readonly property real viewHeight: 78

  // Faces, brightest first.
  readonly property color faceRight: root.color
  readonly property color faceLeft: Qt.darker(root.color, 1.2)
  readonly property color faceBottom: Qt.darker(root.color, 1.4)
  // Folds, where two faces meet.
  readonly property color foldRight: Qt.darker(root.color, 1.58)
  readonly property color foldTop: Qt.darker(root.color, 1.72)
  readonly property color foldLeft: Qt.darker(root.color, 1.86)

  width: iconSize * (viewWidth / viewHeight)
  height: iconSize
  implicitWidth: width
  implicitHeight: height

  Shape {
    x: 0
    y: 0
    width: root.viewWidth
    height: root.viewHeight
    antialiasing: true
    // Curve rendering evaluates the outline per-fragment, so scaling the
    // shape down does not soften its edges the way tessellation would.
    preferredRendererType: Shape.CurveRenderer

    transform: Scale {
      xScale: root.width / root.viewWidth
      yScale: root.height / root.viewHeight
    }

    // Bottom-left fold (brand #0066da)
    ShapePath {
      fillColor: root.foldLeft
      strokeWidth: 0
      PathSvg { path: "m6.6 66.85 3.85 6.65c.8 1.4 1.95 2.5 3.3 3.3l13.75-23.8h-27.5c0 1.55.4 3.1 1.2 4.5z" }
    }

    // Left face (brand #00ac47)
    ShapePath {
      fillColor: root.faceLeft
      strokeWidth: 0
      PathSvg { path: "m43.65 25-13.75-23.8c-1.35.8-2.5 1.9-3.3 3.3l-25.4 44a9.06 9.06 0 0 0 -1.2 4.5h27.5z" }
    }

    // Bottom-right fold (brand #ea4335)
    ShapePath {
      fillColor: root.foldRight
      strokeWidth: 0
      PathSvg { path: "m73.55 76.8c1.35-.8 2.5-1.9 3.3-3.3l1.6-2.75 7.65-13.25c.8-1.4 1.2-2.95 1.2-4.5h-27.502l5.852 11.5z" }
    }

    // Apex fold (brand #00832d)
    ShapePath {
      fillColor: root.foldTop
      strokeWidth: 0
      PathSvg { path: "m43.65 25 13.75-23.8c-1.35-.8-2.9-1.2-4.5-1.2h-18.5c-1.6 0-3.15.45-4.5 1.2z" }
    }

    // Bottom face (brand #2684fc)
    ShapePath {
      fillColor: root.faceBottom
      strokeWidth: 0
      PathSvg { path: "m59.8 53h-32.3l-13.75 23.8c1.35.8 2.9 1.2 4.5 1.2h50.8c1.6 0 3.15-.45 4.5-1.2z" }
    }

    // Right face (brand #ffba00)
    ShapePath {
      fillColor: root.faceRight
      strokeWidth: 0
      PathSvg { path: "m73.4 26.5-12.7-22c-.8-1.4-1.95-2.5-3.3-3.3l-13.75 23.8 16.15 28h27.45c0-1.55-.4-3.1-1.2-4.5z" }
    }
  }
}
