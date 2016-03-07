import bpy
import bpy.props
import mathutils

from mathutils import Vector
from bpy.props import *

import bmesh

import re
import glob
import os

from struct import *

bl_info = {
    "name": "Ninja Ripper mesh data (.rip)",
    "author": "Alexander Gavrilov",
    "version": (0, 1),
    "blender": (2, 77, 0),
    "location": "File > Import-Export > Ninja Ripper (.rip) ",
    "description": "Import Ninja Ripper mesh data",
    "warning": "",
    "category": "Import-Export",
}

def read_uint(fh):
    return unpack('I', fh.read(4))[0]

def read_string(fh):
    str = b''
    while True:
        c = fh.read(1)
        if c == b'\0' or c == b'':
            return str.decode('cp437')
        else:
            str = str + c

def concat_attrs(datalists):
    result = []
    for i in range(len(datalists[0])):
        data = []
        for l in datalists:
            data.extend(l[i])
        result.append(data)
    return result

class RipFileAttribute(object):
    def __init__(self, fh):
        self.semantic = read_string(fh)
        self.semantic_index = read_uint(fh)
        self.offset = read_uint(fh)
        self.size = read_uint(fh)
        self.end = self.offset + self.size
        self.items = read_uint(fh)

        format = ''
        codes = ['f', 'I', 'i']
        for j in range(self.items):
            id = read_uint(fh)
            format = format + (codes[id] if id <= 2 else 'I')

        self.format = format
        self.data = []

    def parse_vertex(self, buffer):
        self.data.append(unpack(self.format, buffer[self.offset : self.end]))

    def as_floats(self, arity=4, divisor=1.0):
        if self.format == 'f'*min(arity,self.items):
            return self.data
        elif self.format[0:arity] == 'f'*arity:
            return list(map(lambda v: v[0:arity], self.data))
        else:
            def convert(item):
                return tuple(map(lambda v: float(v)/divisor, item[0:arity]))
            return list(map(convert, self.data))

class RipFile(object):
    def __init__(self, filename):
        self.filename = filename
        self.dirname = os.path.dirname(filename)
        self.faces = []
        self.attributes = []
        self.shaders = []
        self.textures = []
        self.num_verts = 0

    def parse_file(self):
        fh = open(self.filename, "rb")
        try:
            magic = read_uint(fh)
            if magic != 0xDEADC0DE:
                raise RuntimeError("Invalid file magic: %08d" % (magic))

            version = read_uint(fh)
            if version != 4:
                raise RuntimeError("Invalid file version: %d" % (version))

            num_faces = read_uint(fh)
            self.num_verts = read_uint(fh)
            block_size = read_uint(fh)
            num_tex = read_uint(fh)
            num_shaders = read_uint(fh)
            num_attrs = read_uint(fh)

            for i in range(num_attrs):
                self.attributes.append(RipFileAttribute(fh))

            for i in range(num_tex):
                self.textures.append(read_string(fh))

            for i in range(num_shaders):
                self.shaders.append(read_string(fh))

            for i in range(num_faces):
                face = unpack('III', fh.read(4*3))

                # Omit degenerate triangles - they are sometimes used to merge strips
                if face[0] != face[1] and face[1] != face[2] and face[0] != face[2]:
                    self.faces.append(face)

            for i in range(self.num_verts):
                data = fh.read(block_size)
                for attr in self.attributes:
                    attr.parse_vertex(data)
        finally:
            fh.close()

    def find_attrs(self, semantic):
        return [attr for attr in self.attributes if attr.semantic == semantic]

class RipConversion(object):
    def __init__(self):
        self.use_normals = True
        self.use_weights = True
        self.normal_max_int = 255
        self.normal_scale = [(1.0, 0.0)] * 3
        self.uv_max_int = 255
        self.uv_scale = [(1.0, 0.0)] * 2

    def scale_normal(self, comp, val):
        return val * self.normal_scale[comp][0] + self.normal_scale[comp][1]

    def convert_normal(self, rip, vec_id, norm):
        return (self.scale_normal(0,norm[0]), self.scale_normal(1,norm[1]), self.scale_normal(2,norm[2]))

    def find_normals(self, rip):
        return rip.find_attrs('NORMAL')

    def get_normals(self, rip):
        normals = self.find_normals(rip)
        if len(normals) == 0:
            return None

        normdata = normals[0].as_floats(3, self.normal_max_int)
        for i in range(len(normdata)):
            normdata[i] = self.convert_normal(rip, i, normdata[i])
        return normdata

    def scale_uv(self, comp, val):
        return val * self.uv_scale[comp][0] + self.uv_scale[comp][1]

    def convert_uv(self, rip, vec_id, uv):
        return (self.scale_uv(0,uv[0]), self.scale_uv(1,uv[1]))

    def find_uv_maps(self, rip):
        return rip.find_attrs('TEXCOORD')

    def get_uv_maps(self, rip):
        maps = self.find_uv_maps(rip)
        if len(maps) == 0:
            return []

        # Output each pair of UV values as a map
        all_uvs = concat_attrs(list(map(lambda attr: attr.as_floats(4, self.uv_max_int), maps)))

        count = int((len(all_uvs[0])+1)/2)
        result_maps = []
        for i in range(count):
            result_maps.append([])

        for i in range(rip.num_verts):
            data = all_uvs[i]
            for j in range(count):
                pair = data[2*j:2*j+2]
                if len(pair) == 1:
                    pair = (pair[0], 0.0)
                result_maps[j].append(self.convert_uv(rip, i, pair))

        return result_maps

    def find_colors(self, rip):
        return rip.find_attrs('COLOR')

    def get_weight_groups(self, rip):
        indices = rip.find_attrs('BLENDINDICES')
        weights = rip.find_attrs('BLENDWEIGHT')
        if len(indices) == 0 or len(weights) == 0:
            return {}

        all_indices = concat_attrs(list(map(lambda attr: attr.data, indices)))
        all_weights = concat_attrs(list(map(lambda attr: attr.as_floats(), weights)))
        count = min(len(all_indices[0]), len(all_weights[0]))
        groups = {}

        for i in range(rip.num_verts):
            for j in range(count):
                idx = all_indices[i][j]
                weight = all_weights[i][j]
                if weight != 0:
                    if idx not in groups:
                        groups[idx] = {}
                    groups[idx][i] = weight

        return groups

    def find_image_texture(self, fullpath, name):
        try:
            img = bpy.data.images.load(fullpath, True)
            for tex in bpy.data.textures:
                if tex.type == 'IMAGE' and tex.image == img:
                    return tex

            tex = bpy.data.textures.new(name, type='IMAGE')
            tex.image = img
            return tex

        except:
            for tex in bpy.data.textures:
                if tex.type == 'IMAGE' and tex.name == name:
                    return tex

            return bpy.data.textures.new(name, type='IMAGE')

    def convert_object(self, rip, scene, obj_name):
        pos_attrs = rip.find_attrs('POSITION')
        if len(pos_attrs) == 0:
            pos_attrs = rip.attributes[0:1]

        # Create mesh
        mesh = bpy.data.meshes.new(obj_name)
        mesh.from_pydata(pos_attrs[0].as_floats(3), [], rip.faces)

        # Assign normals
        mesh.polygons.foreach_set("use_smooth", [True] * len(rip.faces))

        if self.use_normals:
            normals = self.get_normals(rip)
            if normals is not None:
                mesh.use_auto_smooth = True
                mesh.show_normal_vertex = True
                mesh.show_normal_loop = True
                mesh.normals_split_custom_set_from_vertices(normals)

        mesh.update()

        # Switch to bmesh
        bm = bmesh.new()
        vgroup_names = []
        try:
            bm.from_mesh(mesh)
            bm.verts.ensure_lookup_table()

            # Create UV maps
            uv_maps = self.get_uv_maps(rip)

            for idx,uvdata in enumerate(uv_maps):
                layer = bm.loops.layers.uv.new('uv'+str(idx))

                for i,vert in enumerate(bm.verts):
                    uv = mathutils.Vector(uvdata[i])
                    for loop in vert.link_loops:
                        loop[layer].uv = uv

            # Create color maps
            colors = self.find_colors(rip)

            def add_color_layer(name,cdata):
                layer = bm.loops.layers.color.new(name)
                for i,vert in enumerate(bm.verts):
                    color = mathutils.Vector(cdata[i])
                    for loop in vert.link_loops:
                        loop[layer] = color

            for idx,cattr in enumerate(colors):
                if cattr.items < 3:
                    continue

                cdata = cattr.as_floats(4, 255)
                add_color_layer('color'+str(idx), list(map(lambda v: v[0:3], cdata)))

                if cattr.items == 4:
                    add_color_layer('alpha'+str(idx), list(map(lambda v: (v[3],v[3],v[3]), cdata)))

            # Create weight groups
            if self.use_weights:
                groups = self.get_weight_groups(rip)

                for group in groups.keys():
                    id = len(vgroup_names)
                    vgroup_names.append('blendweight'+str(group))
                    layer = bm.verts.layers.deform.verify()
                    weights = groups[group]

                    for vid in weights.keys():
                        bm.verts[vid][layer][id] = weights[vid]

            bm.to_mesh(mesh)
        finally:
            bm.free()

        # Textures
        if len(rip.textures) > 0:
            mat = bpy.data.materials.new(obj_name)
            mesh.materials.append(mat)

            for i,tex in enumerate(rip.textures):
                fullpath = os.path.join(rip.dirname, tex)
                imgtex = self.find_image_texture(fullpath, tex)

                slot = mat.texture_slots.create(i)
                slot.texture = imgtex
                slot.use = (i == 0)

        # Finalize
        for i in range(len(rip.shaders)):
            mesh["shader_"+str(i)] = rip.shaders[i]

        mesh.update()

        # Create and select object
        for o in scene.objects:
            o.select = False

        nobj = bpy.data.objects.new(obj_name, mesh)

        for vname in vgroup_names:
            nobj.vertex_groups.new(vname)

        scene.objects.link(nobj)
        scene.update()

        nobj.select = True
        scene.objects.active = nobj

        return nobj

class RipImporter(bpy.types.Operator):
    """Load Ninja Ripper mesh data"""
    bl_idname = "import_mesh.rip"
    bl_label = "Import RIP"
    bl_options = {'UNDO'}

    filename_ext = ".rip"
    filter_glob = StringProperty(default="*.rip", options={'HIDDEN'})
    filepath = StringProperty(subtype='FILE_PATH')
    files = CollectionProperty(name="File Path", type=bpy.types.OperatorFileListElement)

    use_normals = BoolProperty(
        default=True, name="Custom Normals",
        description="Import vertex normal data as custom normals"
    )
    normal_int = IntProperty(
        default = 255, name="Max int normal",
        description="Divide by this value if the normal data type is not float"
    )
    normal_mul = FloatVectorProperty(
        size=3,default=(1.0,1.0,1.0),step=1,
        name="Scale",subtype='XYZ',
        description="Multiply the raw normals by these values"
    )
    normal_add = FloatVectorProperty(
        size=3,default=(0.0,0.0,0.0),step=1,
        name="Offset",subtype='TRANSLATION',
        description="Add this to the scaled normal coordinates"
    )

    uv_int = IntProperty(
        default = 255, name="Max int UV",
        description="Divide by this value if the UV data type is not float"
    )
    uv_mul = FloatVectorProperty(
        size=2,default=(1.0,1.0),step=1,
        name="Scale",subtype='XYZ',
        description="Multiply the raw UVs by these values"
    )
    uv_add = FloatVectorProperty(
        size=2,default=(0.0,0.0),step=1,
        name="Offset",subtype='TRANSLATION',
        description="Add this to the scaled UV coordinates"
    )
    uv_flip_y = BoolProperty(
        name = "Flip Vertical",
        description="Additionally apply a 1-V transform"
    )

    use_weights = BoolProperty(
        default=True, name="Blend Weights",
        description="Import vertex blend weight data as vertex groups"
    )

    def draw(self, context):
        self.layout.operator('file.select_all_toggle')

        uv = self.layout.box()
        uv.prop(self, "uv_int")
        row = uv.row()
        row.column().prop(self, "uv_mul")
        row.column().prop(self, "uv_add")
        uv.prop(self, "uv_flip_y")

        norm = self.layout.box()
        norm.prop(self, "use_normals")
        norm.prop(self, "normal_int")
        row = norm.row()
        row.enabled = self.use_normals
        row.column().prop(self, "normal_mul")
        row.column().prop(self, "normal_add")

        self.layout.prop(self, "use_weights")

    def get_normal_scale(self, i):
        return (self.normal_mul[i], self.normal_add[i])

    def get_uv_scale(self, i):
        if self.uv_flip_y and i == 1:
            return (-self.uv_mul[i], 1.0-self.uv_add[i])
        else:
            return (self.uv_mul[i], self.uv_add[i])

    def execute(self, context):
        conv = RipConversion()
        conv.use_normals = self.use_normals
        conv.use_weights = self.use_weights
        conv.normal_max_int = self.normal_int
        conv.normal_scale = list(map(self.get_normal_scale, range(3)))
        conv.uv_max_int = self.uv_int
        conv.uv_scale = list(map(self.get_uv_scale, range(2)))

        dirname = os.path.dirname(self.filepath)

        for file in self.files:
            rf = RipFile(os.path.join(dirname, file.name))
            rf.parse_file()
            conv.convert_object(rf, context.scene, file.name)

        return {'FINISHED'}

    def invoke(self, context, event):
        wm = context.window_manager
        wm.fileselect_add(self)
        return {'RUNNING_MODAL'}


def menu_import(self, context):
    self.layout.operator(RipImporter.bl_idname, text="Ninja Ripper (.rip)")

def register():
    bpy.utils.register_module(__name__)
    bpy.types.INFO_MT_file_import.append(menu_import)

def unregister():
    bpy.utils.unregister_module(__name__)
    bpy.types.INFO_MT_file_import.remove(menu_import)

if __name__ == "__main__":
    register()