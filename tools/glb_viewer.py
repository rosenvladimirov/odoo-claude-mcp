#!/usr/bin/env python3
"""
GLB 3D Viewer — GNOME/libadwaita + GTK4 GLArea + PyOpenGL.
Opens .glb (glTF 2.0 binary) files and renders them with mouse rotation.
"""
import ctypes
import math
import struct
import sys

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk, Gdk, GLib, Gio  # noqa: E402

import numpy as np  # noqa: E402
from OpenGL import GL  # noqa: E402
import pygltflib  # noqa: E402

# ── Color palette for meshes without materials ────────────────────────
PALETTE = [
    (0.82, 0.66, 0.32, 1.0),  # warm wood
    (0.75, 0.60, 0.28, 1.0),  # darker wood
    (0.68, 0.54, 0.24, 1.0),  # deep wood
    (0.85, 0.72, 0.40, 1.0),  # light wood
    (0.55, 0.45, 0.35, 1.0),  # walnut
    (0.90, 0.80, 0.60, 1.0),  # birch
]

COMPONENT_TYPE_SIZE = {
    5120: 1, 5121: 1, 5122: 2, 5123: 2, 5125: 4, 5126: 4,
}
COMPONENT_TYPE_NUMPY = {
    5120: np.int8, 5121: np.uint8, 5122: np.int16,
    5123: np.uint16, 5125: np.uint32, 5126: np.float32,
}
TYPE_COUNT = {
    "SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4,
    "MAT2": 4, "MAT3": 9, "MAT4": 16,
}


def load_glb(path):
    """Load GLB and extract mesh data (positions, normals, indices)."""
    glb = pygltflib.GLTF2().load(path)
    blob = glb.binary_blob()
    meshes = []

    for node_idx in _iter_nodes(glb):
        node = glb.nodes[node_idx]
        if node.mesh is None:
            continue
        mesh = glb.meshes[node.mesh]
        transform = _node_transform(node)

        for prim in mesh.primitives:
            positions = _get_accessor_data(glb, blob, prim.attributes.POSITION)
            normals = _get_accessor_data(glb, blob, prim.attributes.NORMAL) if prim.attributes.NORMAL is not None else None
            indices = _get_accessor_data(glb, blob, prim.indices) if prim.indices is not None else None

            # Apply node transform to positions
            if transform is not None and positions is not None:
                pos4 = np.hstack([positions, np.ones((len(positions), 1), dtype=np.float32)])
                positions = (pos4 @ transform.T)[:, :3]
                if normals is not None:
                    norm_mat = np.linalg.inv(transform[:3, :3]).T
                    normals = (normals @ norm_mat.T)
                    norms = np.linalg.norm(normals, axis=1, keepdims=True)
                    norms[norms == 0] = 1
                    normals = normals / norms

            # Material color
            color = PALETTE[len(meshes) % len(PALETTE)]
            if prim.material is not None and prim.material < len(glb.materials):
                mat = glb.materials[prim.material]
                if mat.pbrMetallicRoughness and mat.pbrMetallicRoughness.baseColorFactor:
                    color = tuple(mat.pbrMetallicRoughness.baseColorFactor)

            meshes.append({
                "name": mesh.name or f"mesh_{len(meshes)}",
                "positions": positions,
                "normals": normals,
                "indices": indices.flatten().astype(np.uint32) if indices is not None else None,
                "color": color,
            })
    return meshes


def _iter_nodes(glb):
    """Yield all node indices in scene order."""
    if not glb.scenes:
        return
    scene = glb.scenes[glb.scene or 0]
    stack = list(scene.nodes or [])
    while stack:
        idx = stack.pop(0)
        yield idx
        node = glb.nodes[idx]
        if node.children:
            stack.extend(node.children)


def _node_transform(node):
    """Compute 4x4 transform matrix from node TRS or matrix."""
    if node.matrix:
        return np.array(node.matrix, dtype=np.float32).reshape(4, 4)
    mat = np.eye(4, dtype=np.float32)
    if node.scale:
        s = node.scale
        mat = mat @ np.diag([s[0], s[1], s[2], 1.0]).astype(np.float32)
    if node.rotation:
        q = node.rotation  # [x, y, z, w]
        mat = mat @ _quat_to_mat4(q)
    if node.translation:
        t = np.eye(4, dtype=np.float32)
        t[:3, 3] = node.translation
        mat = t @ mat
    if np.allclose(mat, np.eye(4)):
        return None
    return mat


def _quat_to_mat4(q):
    x, y, z, w = q
    m = np.eye(4, dtype=np.float32)
    m[0, 0] = 1 - 2*(y*y + z*z)
    m[0, 1] = 2*(x*y - z*w)
    m[0, 2] = 2*(x*z + y*w)
    m[1, 0] = 2*(x*y + z*w)
    m[1, 1] = 1 - 2*(x*x + z*z)
    m[1, 2] = 2*(y*z - x*w)
    m[2, 0] = 2*(x*z - y*w)
    m[2, 1] = 2*(y*z + x*w)
    m[2, 2] = 1 - 2*(x*x + y*y)
    return m


def _get_accessor_data(glb, blob, accessor_idx):
    if accessor_idx is None:
        return None
    acc = glb.accessors[accessor_idx]
    bv = glb.bufferViews[acc.bufferView]
    offset = (bv.byteOffset or 0) + (acc.byteOffset or 0)
    dtype = COMPONENT_TYPE_NUMPY[acc.componentType]
    count = acc.count * TYPE_COUNT[acc.type]
    data = np.frombuffer(blob, dtype=dtype, count=count, offset=offset)
    cols = TYPE_COUNT[acc.type]
    if cols > 1:
        data = data.reshape(-1, cols)
    return data.copy()


# ── OpenGL shaders ────────────────────────────────────────────────────

VERT_SHADER = """
#version 330 core
layout(location=0) in vec3 aPos;
layout(location=1) in vec3 aNorm;
uniform mat4 uMVP;
uniform mat3 uNormalMat;
out vec3 vNorm;
out vec3 vPos;
void main() {
    gl_Position = uMVP * vec4(aPos, 1.0);
    vNorm = normalize(uNormalMat * aNorm);
    vPos = aPos;
}
"""

FRAG_SHADER = """
#version 330 core
in vec3 vNorm;
in vec3 vPos;
uniform vec4 uColor;
uniform vec3 uLightDir;
out vec4 fragColor;
void main() {
    float ambient = 0.3;
    float diff = max(dot(normalize(vNorm), uLightDir), 0.0) * 0.7;
    vec3 col = uColor.rgb * (ambient + diff);
    fragColor = vec4(col, uColor.a);
}
"""


def _compile_shader(src, shader_type):
    s = GL.glCreateShader(shader_type)
    GL.glShaderSource(s, src)
    GL.glCompileShader(s)
    if not GL.glGetShaderiv(s, GL.GL_COMPILE_STATUS):
        raise RuntimeError(GL.glGetShaderInfoLog(s).decode())
    return s


def _create_program():
    vs = _compile_shader(VERT_SHADER, GL.GL_VERTEX_SHADER)
    fs = _compile_shader(FRAG_SHADER, GL.GL_FRAGMENT_SHADER)
    prog = GL.glCreateProgram()
    GL.glAttachShader(prog, vs)
    GL.glAttachShader(prog, fs)
    GL.glLinkProgram(prog)
    if not GL.glGetProgramiv(prog, GL.GL_LINK_STATUS):
        raise RuntimeError(GL.glGetProgramInfoLog(prog).decode())
    GL.glDeleteShader(vs)
    GL.glDeleteShader(fs)
    return prog


# ── Perspective / view matrices ───────────────────────────────────────

def _perspective(fov, aspect, near, far):
    f = 1.0 / math.tan(math.radians(fov) / 2)
    m = np.zeros((4, 4), dtype=np.float32)
    m[0, 0] = f / aspect
    m[1, 1] = f
    m[2, 2] = (far + near) / (near - far)
    m[2, 3] = (2 * far * near) / (near - far)
    m[3, 2] = -1
    return m


def _look_at(eye, center, up):
    f = np.array(center) - np.array(eye)
    f = f / np.linalg.norm(f)
    u = np.array(up, dtype=np.float32)
    s = np.cross(f, u)
    s = s / np.linalg.norm(s)
    u = np.cross(s, f)
    m = np.eye(4, dtype=np.float32)
    m[0, :3] = s
    m[1, :3] = u
    m[2, :3] = -f
    t = np.eye(4, dtype=np.float32)
    t[:3, 3] = -np.array(eye, dtype=np.float32)
    return m @ t


def _rotate_y(angle):
    c, s = math.cos(angle), math.sin(angle)
    m = np.eye(4, dtype=np.float32)
    m[0, 0] = c; m[0, 2] = s
    m[2, 0] = -s; m[2, 2] = c
    return m


def _rotate_x(angle):
    c, s = math.cos(angle), math.sin(angle)
    m = np.eye(4, dtype=np.float32)
    m[1, 1] = c; m[1, 2] = -s
    m[2, 1] = s; m[2, 2] = c
    return m


# ── GTK4 Application ─────────────────────────────────────────────────

class GLBViewerApp(Adw.Application):
    def __init__(self, glb_path):
        super().__init__(
            application_id="com.blconsulting.glb_viewer",
            flags=Gio.ApplicationFlags.FLAGS_NONE,
        )
        self.glb_path = glb_path

    def do_activate(self):
        win = GLBViewerWindow(application=self, glb_path=self.glb_path)
        win.present()


class GLBViewerWindow(Adw.ApplicationWindow):
    def __init__(self, glb_path, **kwargs):
        super().__init__(**kwargs)
        self.set_title(f"GLB Viewer — {glb_path.split('/')[-1]}")
        self.set_default_size(900, 700)

        self.meshes = load_glb(glb_path)
        self.gl_ready = False
        self.gpu_meshes = []
        self.program = None

        # Camera
        self.rot_x = 0.2
        self.rot_y = 0.0
        self.zoom = 1.0
        self.drag = False
        self.prev_x = 0
        self.prev_y = 0
        self.auto_rotate = True

        # Compute bounding box for camera
        all_pos = np.vstack([m["positions"] for m in self.meshes if m["positions"] is not None])
        self.center = (all_pos.max(axis=0) + all_pos.min(axis=0)) / 2
        self.radius = np.linalg.norm(all_pos.max(axis=0) - all_pos.min(axis=0)) / 2

        # Layout
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.set_content(box)

        header = Adw.HeaderBar()
        box.append(header)

        info = Gtk.Label(label=f"{len(self.meshes)} meshes, {sum(len(m['positions']) for m in self.meshes)} vertices")
        info.add_css_class("dim-label")
        header.set_title_widget(info)

        btn_reset = Gtk.Button(icon_name="view-refresh-symbolic")
        btn_reset.set_tooltip_text("Reset view")
        btn_reset.connect("clicked", self._on_reset)
        header.pack_end(btn_reset)

        # GL Area
        self.glarea = Gtk.GLArea()
        self.glarea.set_required_version(3, 3)
        self.glarea.set_has_depth_buffer(True)
        self.glarea.set_vexpand(True)
        self.glarea.set_hexpand(True)
        self.glarea.connect("realize", self._on_realize)
        self.glarea.connect("render", self._on_render)
        box.append(self.glarea)

        # Mouse events
        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self._on_drag_begin)
        drag.connect("drag-update", self._on_drag_update)
        self.glarea.add_controller(drag)

        scroll = Gtk.EventControllerScroll(
            flags=Gtk.EventControllerScrollFlags.VERTICAL
        )
        scroll.connect("scroll", self._on_scroll)
        self.glarea.add_controller(scroll)

        # Auto-rotate timer
        GLib.timeout_add(16, self._tick)

    def _on_realize(self, area):
        area.make_current()
        if area.get_error():
            return
        self.program = _create_program()
        self._upload_meshes()
        self.gl_ready = True

    def _upload_meshes(self):
        for m in self.meshes:
            vao = GL.glGenVertexArrays(1)
            GL.glBindVertexArray(vao)

            pos = m["positions"].astype(np.float32).flatten()
            vbo_pos = GL.glGenBuffers(1)
            GL.glBindBuffer(GL.GL_ARRAY_BUFFER, vbo_pos)
            GL.glBufferData(GL.GL_ARRAY_BUFFER, pos.nbytes, pos, GL.GL_STATIC_DRAW)
            GL.glVertexAttribPointer(0, 3, GL.GL_FLOAT, GL.GL_FALSE, 0, None)
            GL.glEnableVertexAttribArray(0)

            if m["normals"] is not None:
                norm = m["normals"].astype(np.float32).flatten()
                vbo_norm = GL.glGenBuffers(1)
                GL.glBindBuffer(GL.GL_ARRAY_BUFFER, vbo_norm)
                GL.glBufferData(GL.GL_ARRAY_BUFFER, norm.nbytes, norm, GL.GL_STATIC_DRAW)
                GL.glVertexAttribPointer(1, 3, GL.GL_FLOAT, GL.GL_FALSE, 0, None)
                GL.glEnableVertexAttribArray(1)

            ebo = None
            idx_count = len(m["positions"])
            if m["indices"] is not None:
                idx = m["indices"]
                idx_count = len(idx)
                ebo = GL.glGenBuffers(1)
                GL.glBindBuffer(GL.GL_ELEMENT_ARRAY_BUFFER, ebo)
                GL.glBufferData(GL.GL_ELEMENT_ARRAY_BUFFER, idx.nbytes, idx, GL.GL_STATIC_DRAW)

            GL.glBindVertexArray(0)
            self.gpu_meshes.append({
                "vao": vao,
                "count": idx_count,
                "indexed": ebo is not None,
                "color": m["color"],
            })

    def _on_render(self, area, ctx):
        if not self.gl_ready:
            return True

        w = area.get_width()
        h = area.get_height() or 1
        GL.glViewport(0, 0, w, h)
        GL.glClearColor(0.94, 0.93, 0.92, 1.0)
        GL.glClear(GL.GL_COLOR_BUFFER_BIT | GL.GL_DEPTH_BUFFER_BIT)
        GL.glEnable(GL.GL_DEPTH_TEST)

        GL.glUseProgram(self.program)

        dist = self.radius * 3.0 * self.zoom
        proj = _perspective(45, w / h, 0.01, dist * 10)
        view = _look_at([0, 0, dist], [0, 0, 0], [0, 1, 0])
        model = _rotate_x(self.rot_x) @ _rotate_y(self.rot_y)

        # Center the model
        center_t = np.eye(4, dtype=np.float32)
        center_t[:3, 3] = -self.center
        model = model @ center_t

        mvp = (proj @ view @ model).astype(np.float32)
        normal_mat = np.linalg.inv(model[:3, :3]).T.astype(np.float32)

        GL.glUniformMatrix4fv(GL.glGetUniformLocation(self.program, "uMVP"), 1, GL.GL_TRUE, mvp)
        GL.glUniformMatrix3fv(GL.glGetUniformLocation(self.program, "uNormalMat"), 1, GL.GL_TRUE, normal_mat)

        light_dir = np.array([0.5, 0.8, 0.6], dtype=np.float32)
        light_dir = light_dir / np.linalg.norm(light_dir)
        GL.glUniform3fv(GL.glGetUniformLocation(self.program, "uLightDir"), 1, light_dir)

        for gm in self.gpu_meshes:
            GL.glUniform4fv(GL.glGetUniformLocation(self.program, "uColor"), 1,
                            np.array(gm["color"], dtype=np.float32))
            GL.glBindVertexArray(gm["vao"])
            if gm["indexed"]:
                GL.glDrawElements(GL.GL_TRIANGLES, gm["count"], GL.GL_UNSIGNED_INT, None)
            else:
                GL.glDrawArrays(GL.GL_TRIANGLES, 0, gm["count"])

        GL.glBindVertexArray(0)
        return True

    def _on_drag_begin(self, gesture, x, y):
        self.auto_rotate = False
        self.prev_x = x
        self.prev_y = y

    def _on_drag_update(self, gesture, dx, dy):
        self.rot_y += dx * 0.005
        self.rot_x += dy * 0.005
        self.rot_x = max(-math.pi / 2, min(math.pi / 2, self.rot_x))
        self.glarea.queue_render()

    def _on_scroll(self, ctrl, dx, dy):
        self.zoom *= 1.1 if dy > 0 else 0.9
        self.zoom = max(0.1, min(10.0, self.zoom))
        self.glarea.queue_render()
        return True

    def _on_reset(self, _btn):
        self.rot_x = 0.2
        self.rot_y = 0.0
        self.zoom = 1.0
        self.auto_rotate = True
        self.glarea.queue_render()

    def _tick(self):
        if self.auto_rotate:
            self.rot_y += 0.008
            self.glarea.queue_render()
        return True


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "/home/rosen/Свалени/comfort-70.glb"
    app = GLBViewerApp(path)
    app.run(None)
