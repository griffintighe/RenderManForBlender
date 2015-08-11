# ##### BEGIN MIT LICENSE BLOCK #####
#
# Copyright (c) 2015 Brian Savery
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.
# 
#
# ##### END MIT LICENSE BLOCK #####

import bpy
import math, mathutils
import os
import time
from mathutils import Matrix, Vector, Quaternion

from . import bl_info

from .util import rib, rib_path, rib_ob_bounds
from .util import make_frame_path
from .util import init_env
from .util import get_sequence_path
from .util import user_path
from .util import path_list_convert, get_real_path
from .util import get_properties, check_if_archive_dirty
from .util import debug
from .util import find_it_path

addon_version = bl_info['version']

# helper functions for parameters
from .nodes import export_shader_nodetree, get_textures
from .nodes import shader_node_rib, get_bxdf_name

# ------------- Atom's helper functions -------------
GLOBAL_ZERO_PADDING = 5
SUPPORTED_INSTANCE_TYPES = ['MESH','CURVE','FONT', 'SURFACE']			# Objects that can be exported as a polymesh via Blender to_mesh() method. ['MESH','CURVE','FONT']
SUPPORTED_DUPLI_TYPES = ['FACES', 'VERTS', 'GROUP']			# Supported dupli types.
MATERIAL_TYPES = ['MESH', 'CURVE','FONT']					# These object types can have materials.
EXCLUDED_OBJECT_TYPES = ['LAMP', 'CAMERA', 'ARMATURE']		# Objects without to_mesh() conversion capabilities.
VOLUMETRIC_LIGHT_TYPES = ['SPOT','AREA','POINT']			# Only these light types affect volumes.
MATERIAL_PREFIX = "mat_"
TEXTURE_PREFIX = "tex_"
MESH_PREFIX = "me_"
CURVE_PREFIX = "cu_"
GROUP_PREFIX = "group_"
MESHLIGHT_PREFIX = "meshlight_"
PSYS_PREFIX = "psys_"
DUPLI_PREFIX = "dupli_"
DUPLI_SOURCE_PREFIX = "dup_src_"

def rounded_tuple(tup):
    return tuple(round(value,4) for value in tup) 

def get_matrix_for_object(passedOb):
    if passedOb.parent:
        mtx = Matrix.Identity(4)
    else:
        mtx = passedOb.matrix_world
    return mtx

#export the instance of an object (dupli)
def export_object_instance(ri, mtx=None, dupli_name = None, 
        instance_handle = None):
    if mtx:
        ri.AttributeBegin()
        ri.Attribute("identifier", {"name": dupli_name})
        ri.Transform(rib(mtx))
        ri.ObjectInstance(instance_handle)
        ri.AttributeEnd()

# ------------- Texture optimisation -------------

# 3Delight specific tdlmake stuff
def make_optimised_texture_3dl(tex, texture_optimiser, srcpath, optpath):
    rm = tex.renderman

    debug("info","Optimising Texture: %s --> %s" % (tex.name, optpath))

    cmd = [texture_optimiser]

    if rm.format == 'ENV_LATLONG':
        cmd.append('-envlatl')
        
    # Wrapping
    cmd.append('-smode')
    cmd.append(rm.wrap_s)
    cmd.append('-tmode')
    cmd.append(rm.wrap_t)
    
    if rm.flip_s:
        cmd.append('-flips')
    if rm.flip_t:
        cmd.append('-flipt')
    
    # Filtering
    if rm.filter_type != 'DEFAULT':
        cmd.append('-filter')
        cmd.append(rm.filter_type)
    if rm.filter_type in ('catmull-rom', 'bessel') and \
            rm.filter_window != 'DEFAULT':
        cmd.append('-window')
        cmd.append(rm.filter_window)

    if rm.filter_width_s != 1.0:
        cmd.append('-sfilterwidth')
        cmd.append(str(rm.filter_width_s))
    if rm.filter_width_t != 1.0:
        cmd.append('-tfilterwidth')
        cmd.append(str(rm.filter_width_t))
    
    if (rm.filter_blur != 1.0):
        cmd.append('-blur')
        cmd.append(str(rm.filter_blur))
    
    # Colour space
    if rm.input_color_space == 'GAMMA':
        cmd.append('-gamma')
        cmd.append(str(rm.input_gamma))
    else:
        cmd.append('-colorspace')
        cmd.append(rm.input_color_space)
    
    # Colour depth
    if rm.output_color_depth == 'UBYTE':
        cmd.append('-ubyte')
    elif rm.output_color_depth == 'SBYTE':
        cmd.append('-sbyte')
    elif rm.output_color_depth == 'USHORT':
        cmd.append('-ushort')
    elif rm.output_color_depth == 'SSHORT':
        cmd.append('-sshort')
    elif rm.output_color_depth == 'FLOAT':
        cmd.append('-float')
        
    if rm.output_compression == 'LZW':
        cmd.append('-lzw')
    elif rm.output_compression == 'ZIP':
        cmd.append('-zip')
    elif rm.output_compression == 'PACKBITS':
        cmd.append('-packbits')
    elif rm.output_compression == 'LOGLUV' and rm.output_color_depth == 'FLOAT':
        cmd.append('-logluv')
    elif rm.output_compression == 'UNCOMPRESSED':
        cmd.append('-c-')  
    
    # add preview
    cmd.append('-preview')
    cmd.append('256')
    
    # Filenames
    cmd.append(srcpath)
    cmd.append(optpath)
    
    
    
    proc = subprocess.Popen(cmd).wait()

# ------------- Filtering -------------

def is_visible_layer(scene, ob):

    for i in range(len(scene.layers)):
        if scene.layers[i] == True and ob.layers[i] == True:
            return True
    return False

def is_renderable(scene, ob):
    return (is_visible_layer(scene, ob) and not ob.hide_render)
    # and not ob.type in ('CAMERA', 'ARMATURE', 'LATTICE'))

def renderable_objects(scene):
    return [ob for ob in scene.objects if is_renderable(scene, ob)]


# ------------- Archive Helpers -------------
# Generate an automatic path to write an archive when 
#'Export as Archive' is enabled
def auto_archive_path(paths, objects, create_folder=False):
    filename = objects[0].name + ".rib"
    
    if os.getenv("ARCHIVE") != None:
        archive_dir = os.getenv("ARCHIVE")
    else:
        archive_dir = os.path.join(paths['export_dir'], "archives")

    if create_folder and not os.path.exists(archive_dir):
        os.mkdir(archive_dir)

    return os.path.join(archive_dir, filename)

def archive_objects(scene):
    archive_obs = []
    
    for ob in renderable_objects(scene):
        # explicitly set
        if ob.renderman.export_archive == True:
            archive_obs.append(ob)
        
        # particle instances
        for psys in ob.particle_systems:
            rm = psys.settings.renderman
            if rm.particle_type == 'OBJECT':
                try:
                    ob = bpy.data.objects[rm.particle_instance_object]
                    archive_obs.append(ob)
                except:
                    pass
                    
        # dupli objects (TODO)

    return archive_obs


# ------------- Data Access Helpers -------------

def get_subframes(segs):
    return [i * 1.0/segs for i in range(segs+1)]

def get_ob_subframes(scene, ob):
    if ob.renderman.motion_segments_override:
        return get_subframes(ob.renderman.motion_segments)
    else:
        return get_subframes(scene.renderman.motion_segments)

def is_subd_last(ob):
    return ob.modifiers and ob.modifiers[len(ob.modifiers)-1].type == 'SUBSURF'

def is_subd_displace_last(ob):
    if len(ob.modifiers) < 2: return False
    
    return (ob.modifiers[len(ob.modifiers)-2].type == 'SUBSURF' and
        ob.modifiers[len(ob.modifiers)-1].type == 'DISPLACE')

def is_subdmesh(ob):
    return (is_subd_last(ob) or is_subd_displace_last(ob))

# XXX do this better, perhaps by hooking into modifier type data in RNA?
# Currently assumes too much is deforming when it isn't
def is_deforming(ob):
    deforming_modifiers = ['ARMATURE', 'CAST', 'CLOTH', 'CURVE', 'DISPLACE', 
                            'HOOK', 'LATTICE', 'MESH_DEFORM', 'SHRINKWRAP', 
                            'SIMPLE_DEFORM', 'SMOOTH', 'WAVE', 'SOFT_BODY', 
                            'SURFACE', 'MESH_CACHE']
    if ob.modifiers:        
        # special cases for auto subd/displace detection
        if len(ob.modifiers) == 1 and is_subd_last(ob):
            return False
        if len(ob.modifiers) == 2 and is_subd_displace_last(ob):
            return False
        
        for mod in ob.modifiers:
            if mod.type in deforming_modifiers:
                return True
    
    return False
    
# handle special case of fluid sim a bit differently
def is_deforming_fluid(ob):
    if ob.modifiers:
        mod = ob.modifiers[len(ob.modifiers)-1]
        if mod.type == 'FLUID_SIMULATION' and mod.settings.type == 'DOMAIN':
            return True



def psys_name(ob, psys):
    return "%s.%s-%s" % (ob.name, psys.name, psys.settings.type)
 
#get a name for the data block.  if it's modified by the obj we need it 
#specified 
def data_name(ob, scene):
    #if this is a blob return the family name
    if ob.type == 'META':
        return ob.name.split('.')[0]

    if ob.data.users > 1 and (ob.is_modified(scene, "RENDER") or \
        ob.is_deform_modified(scene, "RENDER") or\
        ob.renderman.primitive != 'AUTO' or\
        (ob.renderman.motion_segments_override and is_deforming(ob))):
        return "%s.%s-MESH" % (ob.name, ob.data.name)

    else:
        return "%s-MESH" % ob.data.name

def get_name(ob):
    return psys_name(ob) if type(ob) == bpy.types.ParticleSystem \
        else ob.data.name


# ------------- Geometry Access -------------

def get_strands(scene, ob, psys):
    tip_width = psys.settings.renderman.tip_width
    base_width = psys.settings.renderman.base_width
    conwidth = psys.settings.renderman.constant_width
    steps = 2 ** psys.settings.render_step 
    if conwidth:
        widthString = "constantwidth"
        hair_width = psys.settings.renderman.width
        debug("info",widthString, hair_width)
    else:
        widthString = "vertex float width"
        hair_width = []
        
    psys.set_resolution(scene, ob, 'RENDER')
    
    num_parents = len(psys.particles)
    num_children = len(psys.child_particles)
    total_hair_count = num_parents + num_children
    thicknessflag = 0
    width_offset = psys.settings.renderman.width_offset
    
    curve_sets = []

    points = []
    
    vertsArray = []
    nverts = 0
    for pindex in range(total_hair_count):
        vertsInStrand = 0
        #walk through each strand
        for step in range(0, steps + 1):
            pt = psys.co_hair(object=ob, particle_no=pindex, step=step)
            
            if not pt.length_squared == 0:
                points.extend(pt)
                #double the first point
                if vertsInStrand == 0:
                    points.extend(pt)
                    vertsInStrand += 1
                vertsInStrand += 1
            else:
                #this strand ends prematurely
                break
            
        if vertsInStrand > 0:
            #for varying width make the width array
            if not conwidth:
                decr = (base_width - tip_width)/(vertsInStrand - 1)
                hair_width.extend([base_width] + [(base_width - decr * i) for i in range(vertsInStrand-1)] + [tip_width])

            #add the last point again
            points.extend(points[-3:])
            vertsInStrand += 1

            vertsArray.append(vertsInStrand)
            nverts += vertsInStrand
        #debug("info","Exporting ",total_hair_count , "Strands and ", nverts ," Vertices")
        #debug("info", "WIDTH:",widthString, hair_width)
        #debug("info", "VERTARRAY:",vertsArray)

        #if we get more than 100000 vertices, export ri.Curve and reset.  This is to avoid a maxint on the array length
        if nverts > 100000 and nverts == len(points)/3:
            curve_sets.append((vertsArray, points, widthString, hair_width))

            #ri.Basis("CatmullRomBasis", 1, "CatmullRomBasis", 1)
            #ri.Attribute("dice", {"int roundcurve": 1, "int hair": 1})
            #ri.Curves("cubic", vertsArray, "nonperiodic", {"P": rib(points), widthString: hair_width})
            nverts = 0
            points = []
            vertsArray = []
            if not conwidth:
                hair_width = []
                
    if nverts > 3 and nverts == len(points)/3:
        #ri.Basis("CatmullRomBasis", 1, "CatmullRomBasis", 1)
        #ri.Attribute("dice", {"int roundcurve": 1, "int hair": 1})
        #ri.Curves("cubic", vertsArray, "nonperiodic", {"P": rib(points), widthString: hair_width})
        curve_sets.append((vertsArray, points, widthString, hair_width))
    else:
        debug("error", "Strands from, ", psys_name(ob, psys), "could not be exported!")
        
    psys.set_resolution(scene, ob, 'PREVIEW')

    return curve_sets

# only export particles that are alive, 
# or have been born since the last frame
def valid_particle(pa, cfra):
    return not (pa.birth_time > cfra or (pa.birth_time + pa.die_time) < cfra)

def get_particle_bounds(particles, cfra):
    xs = []
    ys = []
    zs = []
    for p in particles:
        if valid_particle(p, cfra):
            xs.append(p.location[0])
            ys.append(p.location[1])
            zs.append(p.location[2])
    return [min(xs), max(xs), min(ys), max(ys), min(zs), max(zs)]

def get_particles(scene, ob, psys, valid_frame=None):
    P = []
    rot = []
    width = []
    
    cfra = scene.frame_current if valid_frame == None else valid_frame
    psys.set_resolution(scene, ob, 'RENDER')
    for pa in [p for p in psys.particles if valid_particle(p, cfra)]:
        P.extend( pa.location )
        rot.extend( pa.rotation )
        
        if pa.alive_state != 'ALIVE':
            width.append(0.0)
        else:
            width.append(pa.size)
    psys.set_resolution(scene, ob, 'PREVIEW')
    return (P, rot, width)

# Mesh data access to support multi-material objects we need to break this up
#by faces
def get_mesh(mesh):
    nverts = []
    verts = []
    P = []
    
    for v in mesh.vertices:
        P.extend( v.co )
  
    for p in mesh.polygons:
        nverts.append( p.loop_total )
        verts.extend( p.vertices )
    
    return (nverts, verts, P)


def get_mesh_vertex_N(mesh):
    N = []
    
    for v in mesh.vertices:
        N.extend( v.normal )
    
    return N

# requires facevertex interpolation
def get_mesh_uv(mesh, name=""):
    uvs = []

    if name == "":
        uv_loop_layer = mesh.uv_layers.active
    else:
        # assuming uv loop layers and uv textures share identical indices
        idx = mesh.uv_textures.keys().index(name)
        uv_loop_layer = mesh.uv_layers[idx]
    
    if uv_loop_layer == None:
        return None
    
    for uvloop in uv_loop_layer.data:
        uvs.append( uvloop.uv.x )
        uvs.append( 1.0 - uvloop.uv.y )     
        # renderman expects UVs flipped vertically from blender

    return uvs


# requires facevertex interpolation
def get_mesh_vcol(mesh, name=""):
    vcol_layer = mesh.vertex_colors[name] if name != "" \
         else mesh.vertex_colors.active
    cols = []
    
    if vcol_layer == None:
        return None
    
    for vcloop in vcol_layer.data:
        cols.extend( vcloop.color )
    
    return cols

# requires per-vertex interpolation
def get_mesh_vgroup(ob, mesh, name=""):
    vgroup = ob.vertex_groups[name] if name != "" else ob.vertex_groups.active
    weights = []
    
    if vgroup == None:
        return None

    for v in mesh.vertices:
        if len(v.groups) == 0:
            weights.append(0.0)
        else:
            weights.extend( [g.weight for g in v.groups \
                    if g.group == vgroup.index ] )
            
    return weights

#if a mesh has more than one material
def is_multi_material(mesh):
    if type(mesh) != bpy.types.Mesh or len(mesh.materials) < 2 \
        or len(mesh.polygons) == 0:
        return False
    first_mat = mesh.polygons[0].material_index
    for p in mesh.polygons:
        if p.material_index != first_mat:
            return True
    return False

def get_primvars(ob, geo, interpolation=""):
    primvars = {}
    if ob.type != 'MESH':
        return primvars
    
    rm = ob.data.renderman

    interpolation = 'facevarying' if interpolation == '' else interpolation

    #get material id if this is a multi-material mesh
    if is_multi_material(geo):
        primvars["uniform float material_id"] = rib([p.material_index \
            for p in geo.polygons])
    
    # default hard-coded prim vars
    if rm.export_smooth_normals and ob.renderman.primitive in \
            ('AUTO', 'POLYGON_MESH', 'SUBDIVISION_MESH'):
        N = get_mesh_vertex_N(geo)
        if N and len(N) > 0:
            primvars["varying normal N"] = N
    if rm.export_default_uv:
        uvs = get_mesh_uv(geo)
        if uvs and len(uvs) > 0:
            primvars["%s float[2] st" % interpolation] = uvs
    if rm.export_default_vcol:
        vcols = get_mesh_vcol(geo)
        if vcols and len(vcols) > 0:
            primvars["%s color Cs" % interpolation] = rib(vcols)
    
    # custom prim vars
    for p in rm.prim_vars:
        if p.data_source == 'VERTEX_COLOR':
            vcols = get_mesh_vcol(geo, p.data_name)
            if vcols and len(vcols) > 0:
                primvars["%s color %s" % (interpolation, p.name)] = rib(vcols)

        elif p.data_source == 'UV_TEXTURE':
            uvs = get_mesh_uv(geo, p.data_name)
            if uvs and len(uvs) > 0:
                primvars["%s float[2] %s" % (interpolation, p.name)] = uvs

        elif p.data_source == 'VERTEX_GROUP':
            weights = get_mesh_vgroup(ob, geo, p.data_name)
            if weights and len(weights) > 0:
                primvars["vertex float %s" % p.name] = weights

    return primvars
    
def get_primvars_particle(scene, psys):
    primvars = {}
    rm = psys.settings.renderman
    cfra = scene.frame_current
    
    for p in rm.prim_vars:
        pvars = []
        
        if p.data_source in ('VELOCITY', 'ANGULAR_VELOCITY'):
            if p.data_source == 'VELOCITY':
                for pa in \
                        [p for p in psys.particles if valid_particle(p, cfra)]:
                    pvars.extend ( pa.velocity )
            elif p.data_source == 'ANGULAR_VELOCITY':
                for pa in \
                        [p for p in psys.particles if valid_particle(p, cfra)]:
                    pvars.extend ( pa.angular_velocity )

            primvars["varying float[3] %s" % p.name] = pvars

        elif p.data_source in \
                ('SIZE', 'AGE', 'BIRTH_TIME', 'DIE_TIME', 'LIFE_TIME'):
            if p.data_source == 'SIZE':
                for pa in \
                        [p for p in psys.particles if valid_particle(p, cfra)]:
                    pvars.append ( pa.size )
            elif p.data_source == 'AGE':
                for pa in \
                        [p for p in psys.particles if valid_particle(p, cfra)]:
                    pvars.append ( (cfra - pa.birth_time) / pa.lifetime )
            elif p.data_source == 'BIRTH_TIME':
                for pa in \
                        [p for p in psys.particles if valid_particle(p, cfra)]:
                    pvars.append ( pa.birth_time )
            elif p.data_source == 'DIE_TIME':
                for pa in \
                        [p for p in psys.particles if valid_particle(p, cfra)]:
                    pvars.append ( pa.die_time )
            elif p.data_source == 'LIFE_TIME':
                for pa in \
                        [p for p in psys.particles if valid_particle(p, cfra)]:
                    pvars.append ( pa.lifetime )

            primvars["varying float %s" % p.name] = pvars

    return primvars



def get_fluid_mesh(scene, ob):
    
    subframe = scene.frame_subframe
    
    fluidmod = [m for m in ob.modifiers if m.type == 'FLUID_SIMULATION'][0]
    fluidmeshverts = fluidmod.settings.fluid_mesh_vertices
    
    mesh = create_mesh(scene, ob)
    (nverts, verts, P) = get_mesh(mesh)
    removeMeshFromMemory(mesh.name)
    
    # use fluid vertex velocity vectors to reconstruct moving points
    P = [P[i] + fluidmeshverts[int(i/3)].velocity[i%3] * subframe * 0.5 for \
        i in range(len(P))]
    
    return (nverts, verts, P)
    
def get_subd_creases(mesh):
    creases = []
    
    # only do creases 1 edge at a time for now, 
    #detecting chains might be tricky..
    for e in mesh.edges:
        if e.crease > 0.0:
            creases.append( (e.vertices[0], e.vertices[1], 
                                e.crease*e.crease * 10) ) 
            # squared, to match blender appareance better 
            #: range 0 - 10 (infinitely sharp)
    return creases

def create_mesh(scene, ob):
    # 2 special cases to ignore:
    # subsurf last or subsurf 2nd last +displace last
    
    #if is_subd_last(ob):
    #    ob.modifiers[len(ob.modifiers)-1].show_render = False
    #elif is_subd_displace_last(ob):
    #    ob.modifiers[len(ob.modifiers)-2].show_render = False
    #    ob.modifiers[len(ob.modifiers)-1].show_render = False
    
    return ob.to_mesh(scene, True, 'RENDER', calc_tessface=True, 
        calc_undeformed=True)    
 
def export_transform(ri, ob, flip_x=False):
    m = ob.parent.matrix_world * ob.matrix_local if ob.parent \
        else ob.matrix_world
    if flip_x:
        m = m.copy()
        m[0] *= -1.0
    ri.Transform(rib(m))

def export_light_source(ri, lamp, shape):
    name = "PxrAreaLight"
    params = {ri.HANDLEID: lamp.name, "float exposure":[lamp.energy], "__instanceid": lamp.name}
    if lamp.type == "HEMI":
        name = "PxrEnvMapLight"  
        params["color envtint"] = rib(lamp.color)
    else:
        params["color lightcolor"] = rib(lamp.color)
        params["string shape"] = shape
    ri.AreaLightSource(name, params)

def export_light_shaders(ri, lamp, do_geometry=True):
    def point():
        ri.Sphere(.1, -.1, .1, 360)

    def geometry(type):
        ri.Geometry(type)

    def spot():
        ri.Disk(0, 0.5, 360)

    shapes = {
            "POINT":("sphere", point),
            "SUN":("disk", lambda: geometry('distantlight')),
            "SPOT":("spot", spot),
            "AREA":("rect", lambda: geometry('rectlight')),
            "HEMI":("env", lambda: geometry('envsphere'))
        }

    handle = lamp.name
    rm = lamp.renderman
    #need this for rerendering
    ri.Attribute('identifier', {'string name': handle})
    #do the shader
    if rm.nodetree != '':
        export_shader_nodetree(ri, lamp, handle)
    else:
        export_light_source(ri, lamp, shapes[lamp.type][0])
    
    #now the geometry
    if do_geometry:
        shapes[lamp.type][1]()

def export_light(rpass, scene, ri, ob):
    lamp = ob.data
    rm = lamp.renderman
    params = []
    
    ri.AttributeBegin()
    export_transform(ri, ob, lamp.type == 'HEMI' or lamp.type == 'SUN')
    ri.ShadingRate(rm.shadingrate)

    export_light_shaders(ri, lamp)
    
    ri.AttributeEnd()
    
    ri.Illuminate(lamp.name, rm.illuminates_by_default)

    
def export_material(ri, mat, handle=None):

    rm = mat.renderman

    if rm.nodetree != '':
        export_shader_nodetree(ri, mat, handle, disp_bound=rm.displacementbound )
    else:
        export_shader(ri, mat)
    
def export_material_archive(ri, mat):
    ri.ReadArchive('material.'+mat.name)
    
    
def export_motion_begin(ri, scene, ob):
    ri.MotionBegin(get_ob_subframes(scene, ob))

def export_hair(ri, scene, ob, psys, data):
    curves = data if data != None else get_strands(scene, ob, psys)
    
    for vertsArray, points, widthString, widths in curves:
        ri.Curves("cubic", vertsArray, "nonperiodic", {"P": rib(points), 
                                            widthString: widths})

def geometry_source_rib(ri, scene, ob):
    rm = ob.renderman
    anim = rm.archive_anim_settings
    blender_frame = scene.frame_current
    
    if rm.geometry_source == 'ARCHIVE':
        archive_path = \
            rib_path(get_sequence_path(rm.path_archive, blender_frame, anim))
        ri.ReadArchive(archive_path)
        
    else:
        if rm.procedural_bounds == 'MANUAL':
            min = rm.procedural_bounds_min
            max = rm.procedural_bounds_max
            bounds = [min[0], max[0], min[1], max[1], min[2], max[2]]
        else:
            bounds = rib_ob_bounds(ob.bound_box)
        
        if rm.geometry_source == 'DELAYED_LOAD_ARCHIVE':
            archive_path = rib_path(get_sequence_path(rm.path_archive, 
                                                        blender_frame, anim))
            ri.Procedural("DelayedReadArchive", archive_path, rib(bounds))
        
        elif rm.geometry_source == 'PROCEDURAL_RUN_PROGRAM':
            path_runprogram = rib_path(rm.path_runprogram)
            ri.Procedural("RunProgram", [path_runprogram, 
                                            rm.path_runprogram_args], 
                                        rib(bounds))
        
        elif rm.geometry_source == 'DYNAMIC_LOAD_DSO':
            path_dso = rib_path(rm.path_dso)
            ri.Procedural("DynamicLoad", [path_dso, rm.path_dso_initial_data], 
                                        rib(bounds))


def export_particle_instances(ri, scene, psys, ob, points):
    rm = psys.settings.renderman
    
    master_ob = bpy.data.objects[rm.particle_instance_object]
    #first call object Begin and read in archive of the master
    master_archive = get_archive_filename(scene, None, data_name( \
        scene.objects[rm.particle_instance_object], scene), 
        relative=True)
    instance_handle = ri.ObjectBegin()
    ri.ReadArchive(master_archive)
    ri.ObjectEnd()

    if rm.use_object_material and len(master_ob.data.materials) > 0:
        export_material_archive(ri, master_ob.data.materials[0].name)
    
    width = rm.width 

    
    num_points = len(points[0][2])
    for i in range(num_points):
        ri.AttributeBegin()
        
        if len(points) > 1:
            export_motion_begin(ri, scene, ob)
        
        for (P, rot, point_width) in points:
            loc = Vector((P[i*3+0], P[i*3+1], P[i*3+2]))
            rotation = Quaternion((rot[i*4+0], rot[i*4+1], rot[i*4+2], rot[i*4+3]))
            scale = width if rm.constant_width else point_width[i]
            mtx = Matrix.Translation(loc) * rotation.to_matrix().to_4x4() \
                    * Matrix.Scale(scale, 4)
            
            ri.Transform(rib(mtx))
        if len(points) > 1:
            ri.MotionEnd()
        
        ri.ObjectInstance(instance_handle)
        ri.AttributeEnd()


def export_particle_points(ri, scene, psys, ob, points):
    rm = psys.settings.renderman
    if len(points) > 1:
        export_motion_begin(ri, scene, ob)

    for (P, rot, width) in points:
        params = {} #get_primvars_particle(scene, psys)
        params[ri.P] =  rib(P)
        params["uniform string type"] = rm.particle_type
        if rm.constant_width:
            params["constantwidth"] = rm.width
        elif rm.export_default_size:
            params["varying float width"] = width
        ri.Points(params)
    
    if len(points) > 1:
        ri.MotionEnd()

#only for emitter types for now 
def export_particles(ri, scene, ob, psys, data=None):

    rm = psys.settings.renderman
    points = data if data != None else [get_particles(scene, ob, psys)]

    # Write object instances or points
    if rm.particle_type == 'OBJECT':
        export_particle_instances(ri, scene, psys, ob, points)
    else:
        export_particle_points(ri, scene, psys, ob, points)

    
def export_comment(ri, comment):
    ri.ArchiveRecord('comment', comment)

def get_texture_list(scene):
    #if not rpass.light_shaders: return
    SUPPORTED_MATERIAL_TYPES = ['MESH','CURVE','FONT', 'SURFACE']
    textures = []
    for o in renderable_objects(scene):
        if o.type == 'CAMERA' or o.type == 'EMPTY':
            continue
        elif o.type == 'LAMP':
            if o.data.renderman.nodetree != '':
                textures = textures + get_textures(o.data)
        elif o.type in SUPPORTED_MATERIAL_TYPES:
            for mat in [mat for mat in o.data.materials if mat != None]:
                textures = textures + get_textures(mat)
        else:
            debug ("error","get_texture_list: unsupported object type [%s]." % o.type)
    return textures

def get_texture_list_preview(scene):
    #if not rpass.light_shaders: return
    textures = []
    return get_textures(find_preview_material(scene))


def export_scene_lights(ri, rpass, scene):
    #if not rpass.light_shaders: return

    export_comment(ri,'##Lights')
    
    for ob in [o for o in rpass.objects if o.type == 'LAMP']:
        export_light(rpass, scene, ri, ob)
    

def export_default_bxdf(ri, name):
    #default bxdf a nice grey plastic
    ri.Bxdf("PxrDisney", "default", {'color baseColor': [0.18, 0.18, 0.18], 'string __instanceid': name})

def export_shader(ri, mat):
    rm = mat.renderman
        
    #if rm.surface_shaders.active == '' or not rpass.surface_shaders: return
    
    name = mat.name
    params = {"color baseColor": rib(mat.diffuse_color),
            "float specular": mat.specular_intensity, 'string __instanceid': mat.name}

    if mat.emit:
        params["color emitColor"] = rib(mat.diffuse_color)
    if mat.subsurface_scattering.use:
        params["float subsurface"] = mat.subsurface_scattering.scale
        params["color subsurfaceColor"] = \
            rib(mat.subsurface_scattering.color)
    if mat.raytrace_mirror.use:
        params["float metallic"] = mat.raytrace_mirror.reflect_factor
    ri.Bxdf("PxrDisney", mat.name, params)
        


def is_smoke(ob):
    for mod in ob.modifiers:
        if mod.type == "SMOKE" and mod.domain_settings:
            return True
    return False 

def detect_primitive(ob):
    if type(ob) == bpy.types.ParticleSystem:
        return ob.settings.type

    rm = ob.renderman
    
    if rm.primitive == 'AUTO':
        if ob.type == 'MESH':
            if is_subdmesh(ob):
                return 'SUBDIVISION_MESH'
            elif is_smoke(ob):
                return 'SMOKE'
            else:
                return 'POLYGON_MESH'
        elif ob.type == 'CURVE':
            return 'CURVE'
        elif ob.type in ('SURFACE', 'FONT'):
            return 'POLYGON_MESH'
        elif ob.type == "META":
            return "META"
        else:
            return 'NONE'
    else:
        return rm.primitive

def get_curve(curve):
    splines = []
    
    for spline in curve.splines:
        P = []
        width = []
        npt = len(spline.bezier_points)*3
        
        for bp in spline.bezier_points:
            P.extend( bp.handle_left )
            P.extend( bp.co )
            P.extend( bp.handle_right )
            width.append( bp.radius * 0.01 )
        
        #basis = ["bezier", 3, "bezier", 3]
        basis = ["BezierBasis", 3, "BezierBasis", 3]
        if spline.use_cyclic_u:
            period = 'periodic'
            # wrap the initial handle around to the end, to begin on the CV
            P = P[3:] + P[:3]
        else:
            period = 'nonperiodic'
            # remove the two unused handles
            npt -= 2
            P = P[3:-3]

        splines.append( (P, width, npt, basis, period) )

    return splines

def export_curve(ri, scene, ob, data):
    if ob.type == 'CURVE':
        curves  = data if data != None else get_curve(ob.data)
        
        for P, width, npt, basis, period in curves:
            ri.Basis(basis[0], basis[1], basis[2], basis[3])
            ri.Curves("cubic", [npt], period, {"P": rib(P), "width": width})
    
    else:
        debug ("error","export_curve: recieved a non-supported object type of [%s]." % ob.type)

def export_subdivision_mesh(ri, scene, ob, data=None):
    mesh = data if data != None else create_mesh(scene, ob)
    
    if is_multi_material(mesh):
        export_multi_material(ri, mesh)

    creases = get_subd_creases(mesh)
    
    (nverts, verts, P)  = get_mesh(mesh)
    #if this is empty continue:
    if nverts == []:
        debug("error empty subdiv mesh %s" % ob.name)
        removeMeshFromMemory(mesh.name)
        return
    tags = []
    nargs = []
    intargs = []
    floatargs = []

    if len(creases) > 0:
        for c in creases:
            tags.append( '"crease"' )
            nargs.extend( [2, 1] )
            intargs.extend( [c[0], c[1]] )
            floatargs.append( c[2] )

    tags.append('interpolateboundary')
    nargs.extend( [0, 0] )
    
    primvars = get_primvars(ob, mesh, "facevarying")
    primvars[ri.P] = P
    
    try:
        ri.SubdivisionMesh("catmull-clark", nverts, verts, tags, nargs, intargs,
            floatargs, primvars)
    except:
        print('sudiv problem', ob.name)
    
    removeMeshFromMemory(mesh.name)

def export_polygon_mesh(ri, scene, ob, data=None):
    debug("info","export_polygon_mesh [%s]" % ob.name)
    
    mesh = data if data != None else create_mesh(scene, ob)
    
    if is_multi_material(mesh):
        export_multi_material(ri, mesh)

    #for multi-material output all those 
    (nverts, verts, P) = get_mesh(mesh)
    #if this is empty continue:
    if nverts == []:
        debug("error empty poly mesh %s" % ob.name)
        removeMeshFromMemory(mesh.name)
        return
    primvars = get_primvars(ob, mesh, "facevarying")
    primvars['P'] = P
    #if this is a multi_material mesh output materials
    ri.PointsPolygons(nverts, verts, primvars)
    removeMeshFromMemory(mesh.name)

def removeMeshFromMemory (passedName):
    # Extra test because this can crash Blender if not done correctly.
    result = False
    mesh = bpy.data.meshes.get(passedName)
    if mesh != None:
        if mesh.users == 0:
            try:
                mesh.user_clear()
                can_continue = True
            except:
                can_continue = False
            
            if can_continue == True:
                try:
                    bpy.data.meshes.remove(mesh)
                    result = True
                except:
                    result = False
            else:
                # Unable to clear users, something is holding a reference to it.
                # Can't risk removing. Favor leaving it in memory instead of risking a crash.
                result = False
    else:
        # We could not fetch it, it does not exist in memory, essentially removed.
        result = True
    return result

def export_points(ri, scene, ob, motion):
    rm = ob.renderman
    
    mesh = create_mesh(scene, ob)
    
    motion_blur = ob.name in motion['deformation']
    
    if motion_blur:
        export_motion_begin(ri,scene, ob)
        samples = motion['deformation'][ob.name]
    else:
        samples = [get_mesh(mesh)]
        
    for nverts, verts, P in samples:
        params = {
            ri.P: rib(P),
            "uniform string type": rm.primitive_point_type,
            "constantwidth": rm.primitive_point_width
        }
        ri.Points(params)
            
    if motion_blur:
        ri.MotionEnd()
            
    removeMeshFromMemory(mesh.name)

#make an ri Volume from the smoke modifier
def export_smoke(ri, ob):
    smoke_modifier = None
    for mod in ob.modifiers:
        if mod.type == "SMOKE":
            smoke_modifier = mod
            break
    smoke_data = smoke_modifier.domain_settings
    #the original object has the modifier too.
    if not smoke_data:
        return
    color_grid = []
    #print(min(smoke_data.flame_grid), max(smoke_data.flame_grid))
    for i in range(int(len(smoke_data.color_grid)/4)):
        color_grid += [smoke_data.color_grid[i*4], smoke_data.color_grid[i*4 + 1], smoke_data.color_grid[i*4+2]]
    params = {
        "varying float density": smoke_data.density_grid,
        "varying float flame": smoke_data.flame_grid,
        "varying color smoke_color": color_grid
    }
    ri.Volume("box", [-1,1,-1,1,-1,1], rib(smoke_data.domain_resolution), params)


def export_sphere(ri, ob):
    rm = ob.renderman
    ri.Sphere(rm.primitive_radius, rm.primitive_zmin, rm.primitive_zmax, 
            rm.primitive_sweepangle)
        
def export_cylinder(ri, ob):
    rm = ob.renderman
    ri.Cylinder(rm.primitive_radius, rm.primitive_zmin, rm.primitive_zmax, 
            rm.primitive_sweepangle)
        
def export_cone(ri, ob):
    rm = ob.renderman
    ri.Cone(rm.primitive_height, rm.primitive_radius, rm.primitive_sweepangle)

def export_disk(ri, ob):
    rm = ob.renderman
    ri.Disk(rm.primitive_height, rm.primitive_radius, rm.primitive_sweepangle)

def export_torus(ri, ob):
    rm = ob.renderman
    ri.Torus(rm.primitive_majorradius, rm.primitive_minorradius, 
            rm.primitive_phimin, rm.primitive_phimax, rm.primitive_sweepangle)

def is_dupli(ob):
    return ob.dupli_type in SUPPORTED_DUPLI_TYPES
    
def is_dupli_source(ob):
    # Is this object the source mesh for other duplis?
    return ob.parent and ob.parent.dupli_type in SUPPORTED_DUPLI_TYPES
 

def export_particle_system(ri, scene, ob, psys, data=None):

    if psys.settings.type == 'EMITTER':
        #particles are always deformation
        export_particles(ri, scene, ob, psys, data)
    else:
        ri.Basis("CatmullRomBasis", 1, "CatmullRomBasis", 1)
        ri.Attribute("dice", {"int roundcurve": 1, "int hair": 1})
        if data != None:
            export_motion_begin(ri, scene, ob)
            for sample in data:
                export_hair(ri, scene, ob, psys, sample)
            ri.MotionEnd()
        else:
            export_hair(ri, scene, ob, psys, data)

#many thanks to @rendermouse for this code
def export_blobby_family(ri, scene, ob):
    family = data_name(ob, scene)
    fam_blobs = [ob for ob in scene.objects if ob.type == 'META' and \
        (ob.name == family or ob.name.split('.')[0] == family)]

    # family master obj
    fam_master = bpy.data.objects.get(family)

    #transform 
    tform = []

    #opcodes
    op = []        
    count = len(fam_blobs)
    for i in range(count):
        op.append(1001) #only blobby ellipsoids for now...
        op.append(i * 16)

    for ob_temp in fam_blobs:
        m = ob_temp.matrix_world

        # multiply only the scale of blobs by 2 (matches Blender threshold=0.800)
        sc = Matrix(((2, 0, 0, 0),
            (0, 2, 0, 0),
            (0, 0, 2, 0),
            (0, 0, 0, 1)))
        m2 = m*sc
        tform = tform + rib(m2)

    op.append(0) #blob operation:add
    op.append(count)
    for n in range(count):
        op.append(n)

    st = ('',)
    parm = {}    

    ri.Blobby(count, op, tform, st, parm)    

    

def export_geometry_data(ri, scene, ob, data=None):
    prim = ob.renderman.primitive if ob.renderman.primitive != 'AUTO' \
        else detect_primitive(ob)
    
    #unsupported type
    if prim == 'NONE':
        debug("WARNING", "Unsupported prim type on %s" % (ob.name))

    if prim == 'SPHERE':
        export_sphere(ri, ob)
    elif prim == 'CYLINDER':
        export_cylinder(ri, ob)
    elif prim == 'CONE':
        export_cone(ri, ob)
    elif prim == 'DISK':
        export_disk(ri, ob)
    elif prim == 'TORUS':
        export_torus(ri, ob)
    
    elif prim == 'META':
        export_blobby_family(ri, scene, ob)

    elif prim == 'SMOKE':
        export_smoke(ri, ob)

    # curve only
    elif prim == 'CURVE' or prim == 'FONT':
        # If this curve is extruded or beveled it can produce faces from a to_mesh call.
        l = ob.data.extrude + ob.data.bevel_depth
        if l > 0:
            export_polygon_mesh(ri, scene, ob, data)
        else:
            export_curve(ri, scene, ob, data) 
 
    # mesh only
    elif prim == 'POLYGON_MESH':
        export_polygon_mesh(ri, scene, ob, data)
    elif prim == 'SUBDIVISION_MESH':
        export_subdivision_mesh(ri, scene, ob, data)
    elif prim == 'POINTS':
        export_points(ri, scene, ob, data)

def empty_motion():
    motion = {}
    motion['transformation'] = {}
    motion['deformation'] = {}
    return motion

#we need the base frame for particles. to conform motion samples
def get_motion_ob(scene, motion, ob, base_frame=None):

    prim = detect_primitive(ob)

    # object transformation animation
    if ob.animation_data != None or ob.constraints:
        if ob.name not in motion['transformation'].keys():
            motion['transformation'][ob.name] = []
        
        if ob.parent:
            mat = ob.parent.matrix_world * ob.matrix_local
        else:
            mat = ob.matrix_world
        
        motion['transformation'][ob.name].append( mat.copy())

    # recursive dupli sub-objects
    #if is_dupli(ob):
    #    ob.dupli_list_create(scene)
    #    dupobs = [(dob.object, dob.matrix) for dob in ob.dupli_list]
    #    for dupob, dupob_mat in dupobs:
    #        if not dupob.hide_render:
    #            get_motion_ob(scene, motion, dupob, base_frame=base_frame)
    #    ob.dupli_list_clear()

    # particles
    for psys in ob.particle_systems:
        pname = psys_name(ob, psys)
        
        if pname not in motion['deformation'].keys():
            motion['deformation'][pname] = []
        
        if psys.settings.type == 'EMITTER':
            motion['deformation'][pname].append( 
                                            get_particles(scene, ob, psys, 
                                                valid_frame=base_frame))
        if psys.settings.type == 'HAIR':
            motion['deformation'][pname].append( get_strands(scene, ob, psys))

    if prim in ('POLYGON_MESH', 'SUBDIVISION_MESH', 'POINTS'):
        # fluid sim deformation - special case
        name = data_name(ob, scene)
        if is_deforming_fluid(ob):
            if name not in motion['deformation'].keys():
                motion['deformation'][name] = []
            
            motion['deformation'][name].append( get_fluid_mesh(scene, ob))          
        
        # deformation animation
        if is_deforming(ob):
            if name not in motion['deformation'].keys():
                motion['deformation'][name] = []
            
            mesh = create_mesh(scene, ob)
            motion['deformation'][name].append(mesh)
            #bpy.data.meshes.remove(mesh)

    # not working yet, needs access to post-deform-modifier curve data
    elif prim == 'CURVE':
        if is_deforming(ob):
            if ob.name not in motion['deformation'].keys():
                motion['deformation'][ob.name] = []
            
            motion['deformation'][ob.name].insert(0, get_curve(ob.data))

# Collect and store motion blur transformation data in a pre-process.
# More efficient, and avoids too many frame updates in blender.
def get_motion(scene):
    motion = empty_motion()
    origframe = scene.frame_current
    
    if not scene.renderman.motion_blur:
        return motion

    # get a de-duplicated set of all possible numbers of motion segments 
    # from renderable objects in the scene, and global scene settings
    all_segs = [ob.renderman.motion_segments for ob in scene.objects \
                                if ob.renderman.motion_segments_override]
    all_segs.append(scene.renderman.motion_segments)
    all_segs = set(all_segs)
    
    # the aim here is to do only a minimal number of scene updates, 
    # so we process objects in batches of equal numbers of segments
    # and update the scene only once for each of those unique fractional 
    #frames per segment set
    for segs in all_segs:
        if segs == scene.renderman.motion_segments:
            motion_obs = [ob for ob in scene.objects \
                                if not ob.renderman.motion_segments_override]
        else:
            motion_obs = [ob for ob in scene.objects \
                                if ob.renderman.motion_segments == segs]

        # prepare list of frames/sub-frames in advance, 
        #ordered from future to present,
        # to prevent too many scene updates 
        #(since loop ends on current frame/subframe)
        for sub in get_subframes(segs):
            scene.frame_set(origframe, sub)
            
            for ob in motion_obs:
                get_motion_ob(scene, motion, ob, base_frame=origframe)
    
    scene.frame_set(origframe, 0)               
    return motion


def export_duplis(ri, scene, ob, motion):
    ob.dupli_list_create(scene, "RENDER")

    #gather list of object masters
    object_masters = {}
    for dupob in ob.dupli_list:
        if dupob.object.name not in object_masters:
            instance_handle = ri.ObjectBegin()
            #export_object_read_archive(ri, scene, dupob.object, motion)
            mat = dupob.object.active_material
            if mat:
                export_material_archive(ri, mat)
            ri.Transform(rib(Matrix.Identity(4)))
            ri.ReadArchive(get_archive_filename(scene, motion, data_name(dupob.object, scene),
            relative=True))
            ri.ObjectEnd()
            object_masters[dupob.object.name] = instance_handle

    for dupob in ob.dupli_list:
        dupli_name = "%s.DUPLI.%s.%d" % (ob.name, dupob.object.name, 
            dupob.index)
        instance_handle = object_masters[dupob.object.name]
        export_object_instance(ri, dupob.matrix, dupli_name, instance_handle)

    ob.dupli_list_clear()

def export_archive(*args):
    pass

#return the filename for a readarchive that this object will be written into 
#objects with attached psys's, probably always need to be animated
def get_archive_filename(scene, motion, name, relative=False):
    path = scene.renderman.path_object_archive_animated if motion != None and \
        name in motion['deformation'] \
        else scene.renderman.path_object_archive_static

    if relative:
        path = user_path(path.replace("$ARC/", ""), scene)
    else:
        path = user_path(path, scene)
    return path.replace('{object}', name)

#here we would export object attributes like holdout, sr, etc
def export_object_attributes(ri, ob):

    #save space! don't export default attribute settings to the RIB

    #shading attributes

    if ob.renderman.do_holdout:
        ri.Attribute("identifier", {"string lpegroup":ob.renderman.lpe_group})    

    if ob.renderman.shading_override:    
        ri.ShadingRate(ob.renderman.shadingrate)

        approx_params = {}
        #output motionfactor always, could not find documented default value?
        approx_params["float motionfactor"] = ob.renderman.geometric_approx_motion

        if ob.renderman.geometric_approx_focus != -1.0:
            approx_params["float focusfactor"] = ob.renderman.geometric_approx_focus

        ri.Attribute("Ri", approx_params)

    #visibility attributes
    vis_params = {}
    if not ob.renderman.visibility_camera:
        vis_params["int camera"] = 0

    if not ob.renderman.visibility_trace_indirect:
        vis_params["int indirect"] = 0

    if not ob.renderman.visibility_trace_transmission:
        vis_params["int transmission"] = 0

    if len(vis_params) > 0 :
        ri.Attribute("visibility", vis_params)
    
    if ob.renderman.matte:
        ri.Matte(ob.renderman.matte)

    #if not bpy.data.meshes[ob.name].show_double_sided:
    #    ri.Sides(1)

    #ray tracing attributes
    if ob.renderman.raytrace_override:

        trace_params = {}

        if ob.renderman.raytrace_maxdiffusedepth != 1:
            trace_params["int maxdiffusedepth"] = ob.renderman.raytrace_maxdiffusedepth

        if ob.renderman.raytrace_maxspeculardepth != 2:
            trace_params["int maxspeculardepth"] = ob.renderman.raytrace_maxspeculardepth

        if not ob.renderman.raytrace_tracedisplacements:
            trace_params["int displacements"] = 0

        if not ob.renderman.raytrace_autobias:
            trace_params["int autobias"] = 0
            if ob.renderman.raytrace_bias != 0.01:
                trace_params["float bias"] = ob.renderman.raytrace_bias

        if ob.renderman.raytrace_samplemotion:
            trace_params["int samplemotion"] = 1

        if ob.renderman.raytrace_decimationrate != 1:
            trace_params["int decimationrate"] = ob.renderman.raytrace_decimationrate

        if ob.renderman.raytrace_intersectpriority != 0:
            trace_params["int intersectpriority"] = ob.renderman.raytrace_intersectpriority

        ri.Attribute("trace", trace_params)

#for each mat in this mesh, call it, then do some shading wizardry to 
#switch between them with PxrBxdfBlend
def export_multi_material(ri, mesh):
    bxdf_names = []
    for mat in mesh.materials:
        export_material_archive(ri, mat)
        bxdf_names.append(get_bxdf_name(mat))

    #first read in the material_id primvar
    ri.Pattern("PxrPrimvar", "read_material_id", {"string varname": \
        "material_id"})

    lobes = []
    masks = []
    #then do an seexpr to set the masks
    for i, bxdf in enumerate(bxdf_names):
        plist = {
            "reference float input": "read_material_id:resultF",
            "string expression": "$input == %d" % i
        }
        ri.Pattern("PxrSeExpr", "mat_mask_%d" % i, plist)
        lobes.append(bxdf)
        masks.append("mat_mask_%d:resultF" % i)

    #finally call PxrBxdfBlend to tie it together
    plist = {"reference bxdf[%d] lobe" % len(bxdf_names): lobes,
        "reference float[%d] mask" % len(bxdf_names): masks
    }
    ri.Bxdf("PxrBxdfBlend", "multi_material", plist)

#get the bounds and expand it a bit if we have a psys
def get_bounding_box(ob):
    bounds = rib_ob_bounds(ob.bound_box)
    return bounds

#export the readarchive for an object or psys if supplied
def export_object_read_archive(ri, scene, ob, motion):
    name = ob.name
    ri.AttributeBegin()
    ri.Attribute("identifier", {"name": name})
    export_object_attributes(ri, ob)
    
    #now the matrix, if we're transforming do the motion here
    if name in motion['transformation']:
        export_motion_begin(ri,scene, ob)
        
        for sample in motion['transformation'][name]:
            ri.Transform(rib(sample))
            
        ri.MotionEnd()
    elif ob.type != 'META':
        export_transform(ri, ob)
    #now the material
    mat = ob.active_material
    if mat:
        export_material_archive(ri, mat)

    #we want these relative paths of the archive
    if ob.data != None:
        archive_filename = get_archive_filename(scene, motion, data_name(ob, scene),
            relative=True)

        bounds = get_bounding_box(ob)
        params = {"string filename": archive_filename, 
            "float[6] bound": bounds}
        ri.Procedural2(ri.Proc2DelayedReadArchive, ri.SimpleBound, params)

    #now the children
    for child in ob.children:
        export_object_read_archive(ri, scene, child, motion)
    ri.AttributeEnd()

#export the readarchive for an object or psys if supplied
def export_particle_read_archive(ri, scene, ob, motion, psys):
    name = psys_name(ob, psys)
    ri.AttributeBegin()
    ri.Attribute("identifier", {"name": name})
    
    #now the material
    mat = ob.material_slots[psys.settings.renderman.material_id - 1].material 
    if mat:
        export_material_archive(ri, mat)

    #we want these relative paths of the archive
    archive_filename = get_archive_filename(scene, motion, name, relative=True)

    ri.ReadArchive(archive_filename)
       
    ri.AttributeEnd()

#export the readarchive for an object or psys if supplied
def export_dupli_read_archive(ri, scene, ob, motion):
    name = ob.name + "-DUPLI"
    ri.AttributeBegin()
    ri.Attribute("identifier", {"name": name})
    
    # if name in motion['transformation']:
    #     export_motion_begin(ri,scene, ob)
       
    #     for sample in motion['transformation'][name]:
    #         ri.Transform(rib(sample))
           
    #     ri.MotionEnd()
    # else:
    #     export_transform(ri, ob)
    #we want these relative paths of the archive
    archive_filename = get_archive_filename(scene, motion, name, relative=True)

    ri.ReadArchive(archive_filename)
       
    ri.AttributeEnd()


#export the archives for an mesh. If this is a 
#deforming mesh we'll need to do more than one
def export_mesh_archive(ri, scene, ob, name, motion, 
        lazy_ribgen):
    
    #if we're doing deformation motion blur, export this frame and next
    archive_filename = get_archive_filename(scene, motion, name)

    #if we cached a deforming mesh get it.
    data = motion['deformation'][name] if name in \
        motion['deformation'] else None

    #if lazy rib gen is on, and archive is up to date..
    #we can skip archiving
    if lazy_ribgen and not check_if_archive_dirty(ob.renderman.update_timestamp, 
                                archive_filename):
        pass
    else:
        ri.Begin(archive_filename)
        #if deformation do motion begin
        
        if data != None:
            export_motion_begin(ri, scene, ob)
            for sample in data:
                export_geometry_data(ri, scene, ob, data=sample)
            ri.MotionEnd()
        else:
            export_geometry_data(ri, scene, ob, data=None)
        #now read in the children
        ri.End()

#export the archives for an mesh. If this is a 
#deforming mesh the particle export will handle it
def export_particle_archive(ri, scene, ob, psys, motion, lazy_ribgen):
    name = psys_name(ob, psys)
    archive_filename = get_archive_filename(scene, motion, name)

    data = motion['deformation'][name] if name in \
        motion['deformation'] else None

    #if lazy rib gen is on, and archive is up to date..
    #we can skip archiving
    if lazy_ribgen and not check_if_archive_dirty(ob.renderman.update_timestamp, 
                                archive_filename):
        pass
    else:
        ri.Begin(archive_filename)
        #particle systems handle motion themselves
        export_particle_system(ri, scene, ob, psys, data=data)
        ri.End()
    

#export the archives for an mesh. If this is a 
#deforming mesh the particle export will handle it
def export_dupli_archive(ri, scene, ob, motion, lazy_ribgen):
    name = ob.name + "-DUPLI"
    archive_filename = get_archive_filename(scene, motion, name)

    #if lazy rib gen is on, and archive is up to date..
    #we can skip archiving
    if lazy_ribgen and not check_if_archive_dirty(ob.renderman.update_timestamp, 
                                archive_filename):
        pass
    else:
        ri.Begin(archive_filename)
        export_duplis(ri, scene, ob, motion)
        ri.End()

#export an archive with all the materials and read it back in
def export_materials_archive(ri, rpass, scene):
    archive_filename = user_path(scene.renderman.path_object_archive_static,
                                 scene).replace('{object}', 'materials')
    ri.Begin(archive_filename)
    for mat_name, mat in bpy.data.materials.items():
        ri.ArchiveBegin('material.' + mat_name)
        export_material(ri, mat)
        ri.ArchiveEnd()
    ri.End()
    
    ri.ReadArchive(os.path.relpath(archive_filename, rpass.paths['archive']))

#take a set of objects and create a set of unique data blocks mapped
#to the list of objects using said block
def map_objects_to_data(objects, scene):
    data_map = {}
    for ob in objects:
        if ob.data == None or ob.type in ['CAMERA', 'LAMP']:
            continue
        name = data_name(ob, scene)
        if name in data_map:
            data_map[name].append(ob)
        else:
            data_map[name] = [ob]

    return data_map


#export all the objects (not cameras or lamps) in a scene
def export_objects(ri, rpass, scene, motion):
    rpass.update_time = time.time()
    lazy_ribgen = scene.renderman.lazy_rib_gen
    objects = renderable_objects(scene)
    data_object_map = map_objects_to_data(objects, scene)
    
    #for each mesh used output an archive
    for mesh_name,objects in data_object_map.items():
        export_mesh_archive(ri, scene, objects[0], mesh_name, 
            motion, lazy_ribgen)
        for ob in objects:
            update_timestamp(rpass, ob)
        
    #particles are their own data block output their archives
    psys_exported = []
    for ob in scene.objects:
        for psys in ob.particle_systems:
            if psys.settings.render_type not in ['OBJECT', 'GROUP']:
                export_particle_archive(ri, scene,ob,psys, motion, lazy_ribgen)
                psys_exported.append((psys, ob))

    #look for duplis
    dupli_obs_exported = []
    for ob in scene.objects:
        #first in object
        if hasattr(ob, 'dupli_type') and ob.dupli_type in SUPPORTED_DUPLI_TYPES:
            export_dupli_archive(ri, scene, ob, motion, lazy_ribgen)
            dupli_obs_exported.append(ob)
        else:
            for psys in ob.particle_systems:
                if psys.settings.render_type in ['OBJECT', 'GROUP']:
                    export_dupli_archive(ri, scene, ob, motion, lazy_ribgen)
                    dupli_obs_exported.append(ob)
                    break
    
    #finally read those objects into the scene    
    for ob in renderable_objects(scene):
        if ob.type in ['CAMERA', 'LAMP']:
            continue
        #for meta balls skip the ones that aren't the family master:
        if ob.type == 'META' and data_name(ob, scene) != ob.name:
            continue
        #particle systems will be exported in here own archive
        if not ob.parent:
            export_object_read_archive(ri, scene, ob, motion)
    
    for psys, ob in psys_exported:
        export_particle_read_archive(ri, scene, ob, motion, psys)

    for dupli_ob in dupli_obs_exported:
        export_dupli_read_archive(ri, scene, dupli_ob, motion)

#update the timestamp on an object from the time the rib writing started:
def update_timestamp(rpass, obj):
    if obj and rpass.update_time:
        obj.renderman.update_timestamp = rpass.update_time

#takes a list of bpy.types.properties and converts to params for rib
def property_group_to_params(node):
    params = {}
    for prop_name,meta in node.prop_meta.items():
        prop = getattr(node, prop_name)
        #if property group recurse
        if meta['renderman_type'] == 'page':
            continue
        #if input socket is linked reference that
        else:
            #if struct is not linked continue
            if 'arraySize' in meta:
                params['%s[%d] %s' % (meta['renderman_type'], len(prop), 
                        meta['renderman_name'])] = rib(prop) 
            else:
                params['%s %s' % (meta['renderman_type'], 
                        meta['renderman_name'])] = \
                    rib(prop, type_hint=meta['renderman_type']) 

    return params

def export_integrator(ri, rpass, scene, preview=False):
    rm = scene.renderman
    integrator = rm.integrator
    if preview or rpass.is_interactive:
        integrator = "PxrPathTracer"

    integrator_settings = getattr(rm, "%s_settings" % integrator)
    params = property_group_to_params(integrator_settings)
    
    ri.Integrator(rm.integrator, "integrator", params)
   

def render_get_resolution(r):
    xres= int(r.resolution_x*r.resolution_percentage*0.01)
    yres= int(r.resolution_y*r.resolution_percentage*0.01)
    return xres, yres


def render_get_aspect(r, camera=None):
    xres, yres = render_get_resolution(r)
    
    xratio= xres*r.pixel_aspect_x/200.0
    yratio= yres*r.pixel_aspect_y/200.0

    if camera == None or camera.type != 'PERSP':
        fit = 'AUTO'
    else:
        fit = camera.sensor_fit
    
    if fit == 'HORIZONTAL' or fit == 'AUTO' and xratio > yratio:
        aspectratio= xratio/yratio
        xaspect= aspectratio
        yaspect= 1.0
    elif fit == 'VERTICAL' or fit == 'AUTO' and yratio > xratio:
        aspectratio= yratio/xratio;
        xaspect= 1.0;
        yaspect= aspectratio;
    else:
        aspectratio = xaspect = yaspect = 1.0
        
    return xaspect, yaspect, aspectratio


def export_render_settings(ri, rpass, scene, preview=False):
    rm = scene.renderman
    r = scene.render
    
    depths = {'int maxdiffusedepth': rm.max_diffuse_depth,
            'int maxspeculardepth': rm.max_specular_depth,
            'int displacements': 1}
    if preview:
        depths = {'int maxdiffusedepth': rm.preview_max_diffuse_depth,
            'int maxspeculardepth': rm.preview_max_specular_depth}

    rpass.resolution = render_get_resolution(r)
    ri.Format(rpass.resolution[0], rpass.resolution[1], 1.0)
    #ri.PixelSamples(rm.pixelsamples_x, rm.pixelsamples_y)
    ri.PixelFilter(rm.pixelfilter, rm.pixelfilter_x, rm.pixelfilter_y)
    ri.ShadingRate(rm.shadingrate )
    ri.Attribute("trace", depths)


def export_camera_matrix(ri, scene, ob, motion):
    
    motion_blur = ob.name in motion['transformation']
    
    if motion_blur:
        export_motion_begin(ri, scene, ob)
        samples = motion['transformation'][ob.name]
    else:
        samples = [ob.matrix_world]
        
    for sample in samples:
            mat = sample
            loc = sample.translation
            rot = sample.to_euler()
            
            s = Matrix(([1,0,0,0],[0,1,0,0],[0,0,-1,0],[0,0,0,1]))
            r = Matrix.Rotation(-rot[0], 4, 'X')
            r *= Matrix.Rotation(-rot[1], 4, 'Y')
            r *= Matrix.Rotation(-rot[2], 4, 'Z')
            l = Matrix.Translation(-loc)
            m = s * r * l

            ri.Transform(rib(m))

    if motion_blur:
        ri.MotionEnd()

def export_camera(ri, scene, motion, camera_to_use=None):
    
    if not scene.camera or scene.camera.type != 'CAMERA':
        return
        
    r = scene.render
    ob = camera_to_use if camera_to_use else scene.camera   
    cam = ob.data
    rm = scene.renderman
    
    xaspect, yaspect, aspectratio = render_get_aspect(r, cam)
    
    if rm.depth_of_field:
        if cam.dof_object:
            dof_distance = (ob.location - cam.dof_object.location).length
        else:
            dof_distance = cam.dof_distance
        ri.DepthOfField(rm.fstop, (cam.lens * 0.001), dof_distance)
        
    if scene.renderman.motion_blur:
        ri.Shutter(rm.shutter_open, rm.shutter_close)
        #ri.Option "shutter" "efficiency" [ %f %f ] \n' % (rm.shutter_efficiency_open, rm.shutter_efficiency_close))

    ri.Clipping(cam.clip_start, cam.clip_end)
    
    if cam.renderman.use_physical_camera:
        #use pxr Camera
        params = property_group_to_params(cam.renderman.PxrCamera_settings)
        if 'float fov' not in params:
            lens= cam.lens
            sensor = cam.sensor_height \
                if cam.sensor_fit == 'VERTICAL' else cam.sensor_width
            params['float fov'] = 360.0*math.atan((sensor*0.5)/lens/aspectratio)/math.pi
        ri.Projection("PxrCamera", params)
    elif cam.type == 'PERSP':
        lens= cam.lens
        
        sensor = cam.sensor_height \
            if cam.sensor_fit == 'VERTICAL' else cam.sensor_width

        fov = 360.0*math.atan((sensor*0.5)/lens/aspectratio)/math.pi
        ri.Projection("perspective", {"fov": fov})
    else:
        lens= cam.ortho_scale
        xaspect= xaspect*lens/(aspectratio*2.0)
        yaspect= yaspect*lens/(aspectratio*2.0)
        ri.Projection("orthographic")

    ri.ScreenWindow(-xaspect, xaspect, -yaspect, yaspect)

    export_camera_matrix(ri, scene, ob, motion)
    
    if camera_to_use:
        ri.Camera("world")
    
def export_camera_render_preview(ri, scene):
    r = scene.render

    xaspect, yaspect, aspectratio = render_get_aspect(r)

    ri.Clipping(0.100000, 100.000000)
    ri.Projection("perspective", {"fov": 28.841546})
    ri.ScreenWindow(-xaspect, xaspect, -yaspect, yaspect)

    ri.Transform([0.685881, -0.317370, -0.654862, 0.000000, 
                0.727634, 0.312469, 0.610666, 0.000000, 
                -0.010817, 0.895343, -0.445245, 0.000000, 
                0.040019, -0.661400, 6.220541, 1.000000])           


def export_searchpaths(ri, paths):
    ri.Option("searchpath", {"string shader": ["%s" % \
        ':'.join(path_list_convert(paths['shader'], to_unix=True))]})
    ri.Option("searchpath", {"string texture": ["%s" % \
        ':'.join(path_list_convert(paths['texture'], to_unix=True))]})
    #need this for multi-material
    ri.Option("searchpath", {"string rixplugin": ["%s" % \
        ':'.join(path_list_convert(paths['rixplugin'], to_unix=True))]})
    
    #ri.Option("searchpath", {"string procedural": ["%s" % \
    #    ':'.join(path_list_convert(paths['procedural'], to_unix=True))]})
    ri.Option("searchpath", {"string archive": paths['archive']})

def export_header(ri):
    render_name = os.path.basename(bpy.data.filepath)
    export_comment(ri, 'Generated by PRMan for Blender, v%s.%s.%s \n' % (addon_version[0], addon_version[1], addon_version[2]))
    export_comment(ri, 'From File: %s on %s\n' % (render_name, time.strftime("%A %c")))
    
    
def find_preview_material(scene):
    for o in renderable_objects(scene):
        if o.type not in ('MESH', 'EMPTY'):
            continue
        if len(o.data.materials) > 0:
            mat = o.data.materials[0]
            if mat != None and mat.name == 'preview':
                return mat

# --------------- Hopefully temporary --------------- #

def get_instance_materials(ob):
    obmats = []
    # Grab materials attached to object instances ...
    if hasattr(ob, 'material_slots'):
        for ms in ob.material_slots:
            obmats.append(ms.material)
    # ... and to the object's mesh data
    if hasattr(ob.data, 'materials'):
        for m in ob.data.materials:
            obmats.append(m)
    return obmats

def find_preview_material(scene):
    # taken from mitsuba exporter
    objects_materials = {}

    for object in renderable_objects(scene):
        for mat in get_instance_materials(object):
            if mat is not None:
                if not object.name in objects_materials.keys(): 
                    objects_materials[object] = []
                objects_materials[object].append(mat)

    # find objects that are likely to be the preview objects
    preview_objects = [o for o in objects_materials.keys() \
                        if o.name.startswith('preview')]
    if len(preview_objects) < 1:
        return

    # find the materials attached to the likely preview object
    likely_materials = objects_materials[preview_objects[0]]
    if len(likely_materials) < 1:
        return

    return likely_materials[0]
    
# --------------- End Hopefully temporary --------------- #

def preview_model(ri,mat):
    if mat.preview_render_type == 'SPHERE':
        ri.Sphere(1, -1, 1, 360)
    elif mat.preview_render_type == 'FLAT': #FLAT PLANE
        #ri.Scale(0.75, 0.75, 0.75)
        ri.Translate(0.0, 0.0, 0.01)
        ri.PointsPolygons([4,], 
            [0, 1, 2, 3],
            {ri.P: [0, -1, -1,  0, 1, -1,  0, 1, 1,  0, -1, 1]})
    else: # CUBE
        ri.Scale(0.75, 0.75, 0.75)
        ri.Translate(0.0,0.0,0.01)
        ri.PointsPolygons([4, 4, 4, 4, 4, 4, ],
            [0, 1, 2, 3, 4, 7, 6, 5, 0, 4, 5, 1,
             1, 5, 6, 2, 2, 6, 7, 3, 4, 0, 3, 7],
            {ri.P: [1, 1, -1, 1, -1, -1, -1, -1, -1, -1, 1, -1, 
                    1, 1, 1, 1, -1, 1, -1, -1, 1, -1, 1, 1]})

    
    

def export_display(ri, rpass, scene):
    rm = scene.renderman
    
    active_layer = scene.render.layers.active
    aovs = [
        #(name, do?, declare type/name, source)
        ("z", active_layer.use_pass_z, None, None),
        ("N", active_layer.use_pass_normal, None, None),
        ("dPdtime", active_layer.use_pass_vector, None, None),
        ("u,v", active_layer.use_pass_uv, None, None),
        ("id", active_layer.use_pass_uv, "float", None),
        #("lpe:shadows", active_layer.use_pass_shadow, "color", None),
        #("reflection", active_layer.use_pass_shadow, "float id"),
        ("lpe:diffuse", active_layer.use_pass_diffuse_direct, "color", None),
        #("lpe:diffusedirect", active_layer.use_pass_diffuse_direct, "color", None),
        ("lpe:indirectdiffuse", active_layer.use_pass_diffuse_indirect, 
            "color", None),
        ("albedo", active_layer.use_pass_diffuse_color, "color", 
            "color lpe:nothruput;noinfinitecheck;noclamp;unoccluded;overwrite;C(U2L)|O"),
        ("lpe:specular", active_layer.use_pass_specular, "color", None),
        #("lpe:diffuse", active_layer.use_pass_diffuse_direct, "color", None),
        ("lpe:indirectspecular", active_layer.use_pass_glossy_indirect, 
            "color", None),
        #specular COLOR???("lpe:indirectdiffuse", active_layer.use_pass_diffuse_indirect, "color", None),
        ("lpe:subsurface", active_layer.use_pass_subsurface_indirect, 
            "color", None),
        ("lpe:refraction", active_layer.use_pass_refraction, "color", None),
        ("lpe:emission", active_layer.use_pass_emit, "color", None),
        #("lpe:ambient occlusion", active_layer.use_pass_emit, "color", None),
        ("allshadows", rm.holdout_settings.do_collector_shadow, "color", "color lpe:holdout;shadowcollector"),
        ("allreflections", rm.holdout_settings.do_collector_reflection, "color", "color lpe:holdout;reflectioncollector"),
        ("allindirectdiffuse", rm.holdout_settings.do_collector_indirectdiffuse, "color", "color lpe:holdout;indirectdiffusecollector"),
        ("allsubsurface", rm.holdout_settings.do_collector_subsurface, "color", "color lpe:holdout;subsurfacecollector"),
        ("allrefractions", rm.holdout_settings.do_collector_refraction, "color", "color lpe:holdout;refractioncollector")
    ]

    #Set bucket shape.
    if rpass.is_interactive:
        ri.Option("bucket", {"string order": [ 'spiral']})

    elif rm.bucket_shape == 'SPIRAL':
        settings = scene.render

        if rm.bucket_sprial_x <= settings.resolution_x and rm.bucket_sprial_y <= settings.resolution_y:
            if rm.bucket_sprial_x == -1 and rm.bucket_sprial_y == -1:
                ri.Option("bucket", {"string order": [ rm.bucket_shape.lower() ]})
            elif rm.bucket_sprial_x == -1:
                halfX = settings.resolution_x / 2
                debug("info", halfX)
                ri.Option("bucket", {"string order": [ rm.bucket_shape.lower() ], "orderorigin": [int(halfX) ,rm.bucket_sprial_y]})
            elif rm.bucket_sprial_y == -1:
                halfY = settings.resolution_y / 2
                ri.Option("bucket", {"string order": [ rm.bucket_shape.lower() ], "orderorigin": [rm.bucket_sprial_y, int(halfY) ]})
            else:
                ri.Option("bucket", {"string order": [ rm.bucket_shape.lower() ], "orderorigin": [rm.bucket_sprial_x ,rm.bucket_sprial_y]})
        else:
            debug("info", "OUTSLIDE LOOP")
            ri.Option("bucket", {"string order": [ rm.bucket_shape.lower() ]})
    else:
        ri.Option("bucket", {"string order": [ rm.bucket_shape.lower() ]})
    #declare display channels
    for aov, doit, declare_type, source in aovs:
        if doit and declare_type:
            params = {}
            if source:
                params['string source'] = source
            ri.DisplayChannel('%s %s' % (declare_type, aov), params)

    if(rm.display_driver == 'it'):
        if find_it_path() == None:
            debug("error", "RMS is not installed IT not available!")
            dspy_driver = 'multires'
        else:
            dspy_driver = rm.display_driver
    else:
        dspy_driver = rm.display_driver

    
    main_display = user_path(rm.path_display_driver_image, 
                scene=scene)
    image_base, ext = main_display.rsplit('.', 1)
    ri.Display(main_display, dspy_driver, "rgba", 
                {"quantize": [0, 0, 0, 0]})

    #now do aovs
    for aov, doit, declare, source in aovs:
        if doit:
            params = {"quantize": [0, 0, 0, 0]}
            if source and 'holdout' in source:
                params['int asrgba'] = 1
            ri.Display('+' + image_base + '.%s.' % aov + ext, dspy_driver, aov, params)

    if rm.do_denoise and not rpass.is_interactive:
        #add display channels for denoising
        denoise_aovs = [
        #(name, declare type/name, source, statistics, filter)
            ("Ci", 'color', None, None, None),
            ("a", 'float', None, None, None),
            ("mse", 'color', 'color Ci', 'mse', None),
            ("albedo", 'color', 'lpe:nothruput;noinfinitecheck;noclamp;unoccluded;overwrite;C(U2L)|O', None, None), 
            ("diffuse", 'color', 'color lpe:C(D[DS]*[LO])|O', None, None), 
            ("diffuse_mse", 'color', 'color lpe:C(D[DS]*[LO])|O', 'mse', None), 
            ("specular", 'color', 'color lpe:CS[DS]*[LO]', None, None), 
            ("specular_mse", 'color', 'color lpe:CS[DS]*[LO]', 'mse', None), 
            ("z", 'float', 'float z', None, True), 
            ("z_var", 'float', 'float z', "variance", True), 
            ("normal", 'normal', 'normal Nn', None, None), 
            ("normal_var", 'normal', 'normal Nn', "variance", None), 
            ("forward", 'vector', 'vector motionFore', None, None), 
            ("backward", 'vector', 'vector motionBack', None, None)
        ]

        for aov, declare_type, source, statistics, do_filter in denoise_aovs:
            params = {}
            if source:
                params['string source'] = source
            if statistics:
                params['string statistics'] = statistics
            if do_filter:
                params['string filter'] = rm.pixelfilter    
            ri.DisplayChannel('%s %s' % (declare_type, aov), params)

        #output denoise_data.exr
        ri.Display('+' + image_base + '.denoise.exr', 'openexr', 
            "Ci,a,mse,albedo,diffuse,diffuse_mse,specular,specular_mse,z,z_var,normal,normal_var,forward,backward",
            {"int asrgba": 1})
        



def export_hider(ri, rpass, scene, preview=False):
    rm = scene.renderman
    
    pv = rm.pixel_variance
    hider_params = {'string integrationmode': 'path', 
                    'int maxsamples': rm.max_samples,
                    'int minsamples': rm.min_samples,
                    'int incremental': 1}

    if preview or rpass.is_interactive:
        hider_params['int maxsamples'] = rm.preview_max_samples
        hider_params['int minsamples'] = rm.preview_min_samples
        pv = rm.preview_pixel_variance

    ri.PixelVariance(pv)

    if rm.light_localization:
        ri.Option("shading",  {"int directlightinglocalizedsampling":4})

    if rm.do_denoise:
        hider_params['string pixelfiltermode'] = 'importance'
    
    if rm.hider == 'raytrace':
        ri.Hider(rm.hider, hider_params)


def write_rib(rpass, scene, ri):
    #info_callback('Generating RIB')
    
    # precalculate motion blur data
    rpass.motion_blur = None
    rpass.objects = renderable_objects(scene)
    rpass.archives = []

    motion = get_motion(scene)

    export_header(ri)
    export_searchpaths(ri, rpass.paths)
    
    export_display(ri, rpass, scene)
    export_hider(ri, rpass, scene)
    export_integrator(ri, rpass, scene)
    
    #export_inline_rib(ri, rpass, scene)
    scene.frame_set(scene.frame_current)
    ri.FrameBegin(scene.frame_current)
    
    export_camera(ri, scene, motion)
    export_render_settings(ri, rpass, scene)
    #export_global_illumination_settings(ri, rpass, scene)
    
    ri.WorldBegin()

    #export_global_illumination_lights(ri, rpass, scene)
    #export_world_coshaders(ri, rpass, scene) # BBM addition
    
    export_scene_lights(ri, rpass, scene)

    export_default_bxdf(ri, "default")
    export_materials_archive(ri, rpass, scene)
    export_objects(ri, rpass, scene, motion)
    
    ri.WorldEnd()

    ri.FrameEnd()
    
def write_preview_rib(rpass, scene, ri):
    preview_rib_data_path = \
            rib_path(os.path.join(os.path.dirname(os.path.realpath(__file__)),
                                    'preview', "preview_scene.rib"))
        
    export_header(ri)
    export_searchpaths(ri, rpass.paths)
    
    # temporary tiff display to be read back into blender render result
    ri.FrameBegin(1)
    ri.Display(os.path.basename(rpass.paths['render_output']), "tiff", "rgb",
                                {ri.DISPLAYQUANTIZE: [0, 0, 0, 0]})
    

    export_hider(ri, rpass, scene, preview=True)
    export_integrator(ri, rpass, scene, preview=True)
    

    export_camera_render_preview(ri, scene)
    export_render_settings(ri, rpass, scene, preview=True)

    ri.WorldBegin()
    
    # preview scene: walls, lights
    ri.ReadArchive(preview_rib_data_path)
    
    # preview model and material
    ri.AttributeBegin()
    ri.Attribute("identifier", {"name":[ "Preview" ]})
    ri.Translate(0,0,0.75)
    
    mat = find_preview_material(scene)
    export_material(ri, mat, 'preview')
    preview_model(ri,mat)
    ri.AttributeEnd()
    
    ri.WorldEnd()
    ri.FrameEnd()



def anim_archive_path(filepath, frame):
    if filepath.find("#") != -1:
        ribpath = make_frame_path(filepath, fr)
    else:
        ribpath = os.path.splitext(filepath)[0] + "." + str(frame).zfill(4) + \
                    os.path.splitext(filepath)[1]
    return ribpath


def write_auto_archives(paths, scene, info_callback):
    for ob in archive_objects(scene):
        export_archive(scene, [ob], archive_motion=True, 
                frame_start=scene.frame_current, frame_end=scene.frame_current)

def interactive_initial_rib(rpass, scene, ri, prman):
    ri.Display('rerender', 'it', 'rgba')
    export_hider(ri, rpass, scene, True)

    ri.EditWorldBegin(rpass.paths['rib_output'], {"string rerenderer": "raytrace"})
    ri.Option('rerender', {'int[2] lodrange': [0,3]})      
    
    ri.ArchiveRecord("structure", ri.STREAMMARKER + "_initial")
    prman.RicFlush("_initial", 1, ri.FINISHRENDERING)
    
#flush the current edit
def edit_flush(ri, edit_num, prman):
    ri.ArchiveRecord("structure", ri.STREAMMARKER + "%d" % edit_num)
    prman.RicFlush("%d" % edit_num, 1, ri.SUSPENDRENDERING)

def issue_light_transform_edit(ri, obj):
    lamp = obj.data
    ri.EditBegin('attribute', {'string scopename': obj.data.name})
    export_transform(ri, obj, obj.type == 'LAMP' and (lamp.type == 'HEMI' or lamp.type == 'SUN'))
    ri.EditEnd()
    
def issue_camera_edit(ri, rpass, camera):
    ri.EditBegin('option')
    export_camera(ri, rpass.scene, {'transformation':[]}, camera_to_use=camera)
    ri.EditEnd()

#search this material/lamp for textures to re txmake and do them
def reissue_textures(ri, rpass, mat):
    made_tex = False
    if mat != None:
        textures = get_textures(mat)
        
        files = rpass.convert_textures(textures)
        if len(files) > 0:
            return True
    return False

#return true if an object has an emissive connection
def is_emissive(object):
    if hasattr(object.data, 'materials'):
        #update the light position and shaders if updated
        for mat in object.data.materials:
            if mat != None and mat.renderman.nodetree != '':
                nt = bpy.data.node_groups[mat.renderman.nodetree]
                if 'Output' in nt.nodes and nt.nodes['Output'].inputs['Light'].is_linked:
                    return True
    return False

#test the active object type for edits to do then do them
def issue_transform_edits(rpass, ri, active, prman):
    if active.is_updated:
        rpass.edit_num += 1
        
        edit_flush(ri, rpass.edit_num, prman)
        #only update lamp if shader is update or pos, seperately
        if active.type == 'LAMP':
            lamp = active.data
            issue_light_transform_edit(ri, active)
            
        elif active.type == 'CAMERA' and active.is_updated:
            issue_camera_edit(ri, rpass, active)
        else:
            if is_emissive(active):
                issue_light_transform_edit(ri, active)

def find_material_objs(nt):
    mat = bpy.context.object.active_material
    objs = []
    #return mat, obj
    for obj in bpy.data.objects:
        for slot in obj.material_slots:
            if slot.material == mat:
                objs.append(obj)

    return mat,objs

#test the active object type for edits to do then do them
def issue_shader_edits(rpass, ri, prman, nt=None, node=None):
    if node == None:
        mat,objs = find_material_objs(nt)

        #do an attribute full rebind
        tex_made = False
        if reissue_textures(ri, rpass, mat):
            tex_made = True

        #if texture made flush it
        if tex_made:
            rpass.edit_num += 1
            edit_flush(ri, rpass.edit_num, prman)
        rpass.edit_num += 1
        edit_flush(ri, rpass.edit_num, prman)
        mat,objs = find_material_objs(nt)
        for obj in objs:
            ri.EditBegin('attribute', {'string scopename': obj.name})
            export_material(ri, mat)
            ri.EditEnd()

    else:
        mat = bpy.context.object.active_material
        #if this is a lamp use that for the mat/name
        if mat == None and bpy.data.scenes[0].objects.active.type == 'LAMP':
            mat = bpy.data.scenes[0].objects.active.data
        if mat == None:
            return
        mat_name = mat.name

        #do an attribute full rebind
        tex_made = False
        if reissue_textures(ri, rpass, mat):
            tex_made = True

        #if texture made flush it
        if tex_made:
            rpass.edit_num += 1
            edit_flush(ri, rpass.edit_num, prman)
        rpass.edit_num += 1
        edit_flush(ri, rpass.edit_num, prman)
        ri.EditBegin('instance')
        shader_node_rib(ri, node, mat.name, recurse=False)
        ri.EditEnd()

