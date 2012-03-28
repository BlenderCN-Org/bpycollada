import os
import math
from shutil import rmtree
from tempfile import NamedTemporaryFile
from contextlib import contextmanager

import bpy
from bpy_extras.image_utils import load_image
from mathutils import Matrix, Vector

from collada import Collada
from collada.camera import PerspectiveCamera, OrthographicCamera
from collada.common import DaeError, DaeBrokenRefError
from collada.light import AmbientLight, DirectionalLight, PointLight, SpotLight
from collada.material import Map
from collada.triangleset import TriangleSet
from collada.polylist import Polylist


__all__ = ['load']

VENDOR_SPECIFIC = []
COLLADA_NS = 'http://www.collada.org/2005/11/COLLADASchema'
DAE_NS = {'dae': COLLADA_NS}


def load(op, ctx, filepath=None, **kwargs):
    c = Collada(filepath, ignore=[DaeBrokenRefError])
    impclass = get_import(c)
    imp = impclass(ctx, c, os.path.dirname(filepath), **kwargs)

    for obj in c.scene.objects('geometry'):
        imp.geometry(obj)

    for i, obj in enumerate(c.scene.objects('light')):
        imp.light(obj, i)

    for obj in c.scene.objects('camera'):
        imp.camera(obj)

    return {'FINISHED'}


def get_import(collada):
    for i in VENDOR_SPECIFIC:
        if i.match(collada):
            return i
    return ColladaImport


class ColladaImport(object):
    """ Standard COLLADA importer. """
    def __init__(self, ctx, collada, basedir, **kwargs):
        self._ctx = ctx
        self._collada = collada
        self._kwargs = kwargs
        self._images = {}
        self._namecount = 0
        self._names = {}

    def camera(self, bcam):
        bpy.ops.object.add(type='CAMERA')
        b_obj = self._ctx.object
        b_obj.name = self.name(bcam.original, id(bcam))
        b_obj.matrix_world = _matrix(bcam.matrix)
        b_cam = b_obj.data
        if isinstance(bcam.original, PerspectiveCamera):
            b_cam.type = 'PERSP'
            b_cam.lens_unit = 'DEGREES'
            b_cam.angle = math.radians(max(
                    bcam.xfov or bcam.yfov,
                    bcam.yfov or bcam.xfov))
        elif isinstance(bcam.original, OrthographicCamera):
            b_cam.type = 'ORTHO'
            b_cam.ortho_scale = max(
                    bcam.xmag or bcam.ymag,
                    bcam.ymag or bcam.xmag)
        if bcam.znear:
            b_cam.clip_start = bcam.znear
        if bcam.zfar:
            b_cam.clip_end = bcam.zfar

    def geometry(self, bgeom):
        b_materials = {}
        for sym, matnode in bgeom.materialnodebysymbol.items():
            mat = matnode.target
            b_matname = self.name(mat)
            if b_matname not in bpy.data.materials:
                self.material(mat, b_matname)
            b_materials[sym] = bpy.data.materials[b_matname]

        for i, p in enumerate(bgeom.original.primitives):
            b_mat = b_materials.get(p.material, None)
            b_meshname = self.name(bgeom.original, i)

            if isinstance(p, TriangleSet):
                b_obj = self.geometry_triangleset(
                        p, b_meshname, b_mat)
            elif isinstance(p, Polylist):
                b_obj = self.geometry_triangleset(
                        p.triangleset(), b_meshname, b_mat)
            else:
                continue
            if not b_obj:
                continue

            self._ctx.scene.objects.link(b_obj)
            self._ctx.scene.objects.active = b_obj
            b_obj.matrix_world = _matrix(bgeom.matrix)

            bpy.ops.object.material_slot_add()
            b_obj.material_slots[0].link = 'OBJECT'
            b_obj.material_slots[0].material = b_mat

    def geometry_triangleset(self, triset, b_name, b_mat):
        if b_name in bpy.data.meshes:
            b_mesh = bpy.data.meshes[b_name]
        else:
            if triset.vertex_index is None or \
                    not len(triset.vertex_index):
                return

            b_mesh = bpy.data.meshes.new(b_name)
            b_mesh.vertices.add(len(triset.vertex))
            b_mesh.faces.add(len(triset.vertex_index))
            for vidx, vertex in enumerate(triset.vertex):
                b_mesh.vertices[vidx].co = vertex

            # eekadoodle
            eekadoodle_faces = [v
                    for f in triset.vertex_index
                    for v in _eekadoodle_face(*f)]
            b_mesh.faces.foreach_set('vertices_raw', eekadoodle_faces)

            has_normal = (triset.normal_index is not None)
            has_uv = (len(triset.texcoord_indexset) > 0)

            if has_normal:
                for i, f in enumerate(b_mesh.faces):
                    f.use_smooth = not _is_flat_face(
                            triset.normal[triset.normal_index[i]])
            if has_uv:
                for j in range(len(triset.texcoord_indexset)):
                    self.texcoord_layer(
                            triset,
                            triset.texcoordset[j],
                            triset.texcoord_indexset[j],
                            b_mesh,
                            b_mat)

            b_mesh.update()

        b_obj = bpy.data.objects.new(b_name, b_mesh)
        b_obj.data = b_mesh
        return b_obj

    def light(self, light, i):
        if isinstance(light.original, AmbientLight):
            return
        b_name = self.name(light.original, i)
        if b_name not in bpy.data.lamps:
            if isinstance(light.original, DirectionalLight):
                b_lamp = bpy.data.lamps.new(b_name, type='SUN')
            elif isinstance(light.original, PointLight):
                b_lamp = bpy.data.lamps.new(b_name, type='POINT')
                b_obj = bpy.data.objects.new(b_name, b_lamp)
                self._ctx.scene.objects.link(b_obj)
                b_obj.matrix_world = Matrix.Translation(light.position)
            elif isinstance(light.original, SpotLight):
                b_lamp = bpy.data.lamps.new(b_name, type='SPOT')

    def material(self, mat, b_name):
        effect = mat.effect
        b_mat = bpy.data.materials.new(b_name)
        b_mat.diffuse_shader = 'LAMBERT'
        getattr(self, 'rendering_' + \
                effect.shadingtype)(mat, b_mat)
        bpy.data.materials[b_name].use_transparent_shadows = \
                self._kwargs.get('transparent_shadows', False)
        if effect.emission:
            b_mat.emit = sum(effect.emission[:3]) / 3.0
        self.rendering_transparency(effect, b_mat)
        self.rendering_reflectivity(effect, b_mat)

    def rendering_blinn(self, mat, b_mat):
        effect = mat.effect
        b_mat.specular_shader = 'BLINN'
        self.rendering_diffuse(effect.diffuse, b_mat)
        self.rendering_specular(effect, b_mat)

    def rendering_constant(self, mat, b_mat):
        effect = mat.effect
        b_mat.use_shadeless = True

    def rendering_lambert(self, mat, b_mat):
        effect = mat.effect
        self.rendering_diffuse(effect.diffuse, b_mat)
        b_mat.specular_intensity = 0.0

    def rendering_phong(self, mat, b_mat):
        effect = mat.effect
        b_mat.specular_shader = 'PHONG'
        self.rendering_diffuse(effect.diffuse, b_mat)
        self.rendering_specular(effect, b_mat)

    def rendering_diffuse(self, diffuse, b_mat):
        b_mat.diffuse_intensity = 1.0
        diff = self.color_or_texture(diffuse, b_mat)
        if isinstance(diff, tuple):
            b_mat.diffuse_color = diff
        else:
            diff.use_map_color_diffuse = True

    def rendering_specular(self, effect, b_mat):
        if effect.specular:
            b_mat.specular_intensity = 1.0
            b_mat.specular_color = effect.specular[:3]
        if effect.shininess:
            b_mat.specular_hardness = effect.shininess

    def rendering_reflectivity(self, effect, b_mat):
        if effect.reflectivity and effect.reflectivity > 0:
            b_mat.raytrace_mirror.use = True
            b_mat.raytrace_mirror.reflect_factor = effect.reflectivity
            if effect.reflective:
                refi = self.color_or_texture(effect.reflective, b_mat)
                if isinstance(refi, tuple):
                    b_mat.mirror_color = refi
                else:
                    # TODO use_map_mirror or use_map_raymir ?
                    pass

    def rendering_transparency(self, effect, b_mat):
        if not effect.transparency:
            return
        if isinstance(effect.transparency, float):
            if effect.transparency < 1.0:
                b_mat.use_transparency = True
                b_mat.alpha = effect.transparency
        if self._kwargs.get('raytrace_transparency', False):
            b_mat.transparency_method = 'RAYTRACE'
            b_mat.raytrace_transparency.ior = 1.0
        if isinstance(effect.index_of_refraction, float):
            b_mat.transparency_method = 'RAYTRACE'
            b_mat.raytrace_transparency.ior = effect.index_of_refraction

    def texcoord_layer(self, triset, texcoord, index, b_mesh, b_mat):
        b_mesh.uv_textures.new()
        for i, f in enumerate(b_mesh.faces):
            t1, t2, t3 = index[i]
            tface = b_mesh.uv_textures[-1].data[i]
            # eekadoodle
            if triset.vertex_index[i][2] == 0:
                t1, t2, t3 = t3, t1, t2
            tface.uv1 = texcoord[t1]
            tface.uv2 = texcoord[t2]
            tface.uv3 = texcoord[t3]
            if b_mat and b_mat.name in self._images:
                tface.image = self._images[b_mat.name]

    def color_or_texture(self, color_or_texture, b_mat):
        if isinstance(color_or_texture, Map):
            image = color_or_texture.sampler.surface.image
            mtex = self.try_texture(image, b_mat)
            return mtex or (1., 0., 0.)
        elif isinstance(color_or_texture, tuple):
            return color_or_texture[:3]

    def try_texture(self, c_image, b_mat):
        mtex = None
        with self._tmpwrite(c_image.path, c_image.data) as tmp:
            image = load_image(tmp)
            if image is not None:
                image.pack(True)

                texture = bpy.data.textures.new(name='Kd', type='IMAGE')
                texture.image = image
                mtex = b_mat.texture_slots.add()
                mtex.texture_coords = 'UV'
                mtex.texture = texture
                self._images[b_mat.name] = image
        return mtex

    def name(self, obj, index=0):
        base = '%s-%d' % (obj.id, index)
        if base not in self._names:
            self._namecount += 1
            self._names[base] = '%s-%.4d' % (base[:27], self._namecount)
        return self._names[base]

    @contextmanager
    def _tmpwrite(self, relpath, data):
        with NamedTemporaryFile(suffix='.' + relpath.split('.')[-1]) as out:
            out.write(data)
            out.flush()
            yield out.name


class SketchUpImport(ColladaImport):
    """ SketchUp specific COLLADA import. """

    def rendering_diffuse(self, diffuse, b_mat):
        """ Imports PNG textures with alpha channel. """
        ColladaImport.rendering_diffuse(self, diffuse, b_mat)
        if isinstance(diffuse, Map):
            if b_mat.name in self._images:
                image = self._images[b_mat.name]
                if image.depth == 32:
                    diffslot = None
                    for ts in b_mat.texture_slots:
                        if ts and ts.use_map_color_diffuse:
                            diffslot = ts
                            break
                    if not diffslot:
                        return
                    image.use_premultiply = True
                    diffslot.use_map_alpha = True
                    tex = diffslot.texture
                    tex.use_mipmap = True
                    tex.use_interpolation = True
                    tex.use_alpha = True
                    b_mat.use_transparency = True
                    b_mat.alpha = 0.0
                    if self._kwargs.get('raytrace_transparency', False):
                        b_mat.transparency_method = 'RAYTRACE'
                        b_mat.raytrace_transparency.ior = 1.0

    def rendering_reflectivity(self, effect, b_mat):
        """ There are no reflectivity controls in SketchUp """
        if not self.__class__.test2(effect.xmlnode):
            ColladaImport.rendering_reflectivity(self, effect, b_mat)

    @classmethod
    def match(cls, collada):
        xml = collada.xmlnode
        return cls.test1(xml) or cls.test2(xml)

    @classmethod
    def test1(cls, xml):
        src = [ xml.find('.//dae:instance_visual_scene',
                    namespaces=DAE_NS).get('url') ]
        at = xml.find('.//dae:authoring_tool', namespaces=DAE_NS)
        if at is not None:
            src.append(at.text)
        return any(['SketchUp' in s for s in src if s])

    @classmethod
    def test2(cls, xml):
        et = xml.findall('.//dae:extra/dae:technique',
                namespaces=DAE_NS)
        return len(et) and any([
            t.get('profile') == 'GOOGLEEARTH'
            for t in et])

VENDOR_SPECIFIC.append(SketchUpImport)


def _is_flat_face(normal):
    a = Vector(normal[0])
    for n in normal[1:]:
        dp = a.dot(Vector(n))
        if dp < 0.99999 or dp > 1.00001:
            return False
    return True

def _matrix(matrix):
    m = Matrix(matrix)
    if bpy.app.version < (2, 62):
        m.transpose()
    return m

def _eekadoodle_face(v1, v2, v3):
    return v3 == 0 and (v3, v1, v2, 0) or (v1, v2, v3, 0)
