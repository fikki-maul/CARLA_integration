
import glob
import os
import sys
import argparse
import time
from datetime import datetime
import random
import numpy as np
from matplotlib import cm
import open3d as o3d
import math
import json

try:
    sys.path.append(glob.glob('../carla/dist/carla-*%d.%d-%s.egg' % (
        sys.version_info.major,
        sys.version_info.minor,
        'win-amd64' if os.name == 'nt' else 'linux-x86_64'))[0])
except IndexError:
    pass

import carla

VIRIDIS = np.array(cm.get_cmap('plasma').colors)
VID_RANGE = np.linspace(0.0, 1.0, VIRIDIS.shape[0])
LABEL_COLORS = np.array([
    (255, 255, 255), # None
    (70, 70, 70),    # Building
    (100, 40, 40),   # Fences
    (55, 90, 80),    # Other
    (220, 20, 60),   # Pedestrian
    (153, 153, 153), # Pole
    (157, 234, 50),  # RoadLines
    (128, 64, 128),  # Road
    (244, 35, 232),  # Sidewalk
    (107, 142, 35),  # Vegetation
    (0, 0, 142),     # Vehicle
    (102, 102, 156), # Wall
    (220, 220, 0),   # TrafficSign
    (70, 130, 180),  # Sky
    (81, 0, 81),     # Ground
    (150, 100, 100), # Bridge
    (230, 150, 140), # RailTrack
    (180, 165, 180), # GuardRail
    (250, 170, 30),  # TrafficLight
    (110, 190, 160), # Static
    (170, 120, 50),  # Dynamic
    (45, 60, 150),   # Water
    (145, 170, 100), # Terrain
]) / 255.0 # normalize each channel [0-1] since is what Open3D uses

def lidar_callback(point_cloud, point_list):
    """Prepares a point cloud with intensity
    colors ready to be consumed by Open3D"""
    data = np.copy(np.frombuffer(point_cloud.raw_data, dtype=np.dtype('f4')))
    data = np.reshape(data, (int(data.shape[0] / 4), 4))

    # Isolate the intensity and compute a color for it
    intensity = data[:, -1]
    intensity_col = 1.0 - np.log(intensity) / np.log(np.exp(-0.004 * 100))
    int_color = np.c_[
        np.interp(intensity_col, VID_RANGE, VIRIDIS[:, 0]),
        np.interp(intensity_col, VID_RANGE, VIRIDIS[:, 1]),
        np.interp(intensity_col, VID_RANGE, VIRIDIS[:, 2])]

    # Isolate the 3D data
    points = data[:, :-1]

    # We're negating the y to correclty visualize a world that matches
    # what we see in Unreal since Open3D uses a right-handed coordinate system
    points[:, :1] = -points[:, :1]

    # # An example of converting points from sensor to vehicle space if we had
    # # a carla.Transform variable named "tran":
    # points = np.append(points, np.ones((points.shape[0], 1)), axis=1)
    # points = np.dot(tran.get_matrix(), points.T).T
    # points = points[:, :-1]

    point_list.points = o3d.utility.Vector3dVector(points)
    point_list.colors = o3d.utility.Vector3dVector(int_color)

def semantic_lidar_callback(point_cloud, point_list):
    """Prepares a point cloud with semantic segmentation
    colors ready to be consumed by Open3D"""
    data = np.frombuffer(point_cloud.raw_data, dtype=np.dtype([
        ('x', np.float32), ('y', np.float32), ('z', np.float32),
        ('CosAngle', np.float32), ('ObjIdx', np.uint32), ('ObjTag', np.uint32)]))

    # We're negating the y to correclty visualize a world that matches
    # what we see in Unreal since Open3D uses a right-handed coordinate system
    points = np.array([data['x'], -data['y'], data['z']]).T

    # # An example of adding some noise to our data if needed:
    # points += np.random.uniform(-0.05, 0.05, size=points.shape)

    # Colorize the pointcloud based on the CityScapes color palette
    labels = np.array(data['ObjTag'])
    int_color = LABEL_COLORS[labels]

    # # In case you want to make the color intensity depending
    # # of the incident ray angle, you can use:
    # int_color *= np.array(data['CosAngle'])[:, None]

    point_list.points = o3d.utility.Vector3dVector(points)
    point_list.colors = o3d.utility.Vector3dVector(int_color)
	
def generate_lidar_bp(arg, world, blueprint_library, delta):
    """Generates a CARLA blueprint based on the script parameters"""
    if arg.semantic:
        lidar_bp = world.get_blueprint_library().find('sensor.lidar.ray_cast_semantic')
    else:
        lidar_bp = blueprint_library.find('sensor.lidar.ray_cast')
        if arg.no_noise:
            lidar_bp.set_attribute('dropoff_general_rate', '0.0')
            lidar_bp.set_attribute('dropoff_intensity_limit', '1.0')
            lidar_bp.set_attribute('dropoff_zero_intensity', '0.0')
        else:
            lidar_bp.set_attribute('noise_stddev', '0.2')

    lidar_bp.set_attribute('upper_fov', str(arg.upper_fov))
    lidar_bp.set_attribute('lower_fov', str(arg.lower_fov))
    lidar_bp.set_attribute('channels', str(arg.channels))
    lidar_bp.set_attribute('range', str(arg.range))
    lidar_bp.set_attribute('rotation_frequency', str(1.0 / delta))
    lidar_bp.set_attribute('points_per_second', str(arg.points_per_second))
    return lidar_bp

def add_open3d_axis(vis):
    """Add a small 3D axis on Open3D Visualizer"""
    axis = o3d.geometry.LineSet()
    axis.points = o3d.utility.Vector3dVector(np.array([
        [0.0, 0.0, 0.0],
        [1.0, 0.0, 0.0],
        [0.0, 1.0, 0.0],
        [0.0, 0.0, 1.0]]))
    axis.lines = o3d.utility.Vector2iVector(np.array([
        [0, 1],
        [0, 2],
        [0, 3]]))
    axis.colors = o3d.utility.Vector3dVector(np.array([
        [1.0, 0.0, 0.0],
        [0.0, 1.0, 0.0],
        [0.0, 0.0, 1.0]]))
    vis.add_geometry(axis)
	
def get_bounding_boxes(vehicle, vehicle_lidar):
    """
    Returns 3D bounding box for a vehicle as set of points.
    """
    cords = np.zeros((3, 8))
    extent = vehicle.bounding_box.extent
    transform = vehicle.get_transform()
    transform_lidar = vehicle_lidar.get_transform()
    location = np.array(
	                    [transform.location.x - transform_lidar.location.x,
	                    transform.location.y - transform_lidar.location.y,
	                    transform.location.z - transform_lidar.location.z])
    angle_lidar = np.radians(transform_lidar.rotation.yaw)
    rotation = np.radians(transform.rotation.yaw)-np.radians(transform_lidar.rotation.yaw)
    cords[0, :] = np.array([-extent.x, -extent.x, extent.x, extent.x, -extent.x, -extent.x, extent.x, extent.x])
    cords[1, :] = np.array([extent.y, -extent.y, -extent.y, extent.y, extent.y, -extent.y, -extent.y, extent.y])
    cords[2, :] = np.array([-extent.z, -extent.z, -extent.z, -extent.z, extent.z, extent.z, extent.z, extent.z])

    rotation_matrix = np.array([[math.cos(rotation), -math.sin(rotation), 0.0], [math.sin(rotation), math.cos(rotation), 0.0], [0.0, 0.0, 1.0]])
    dist = math.sqrt(location[0]**2 + location[1]**2)
    if dist>0:
        lidar_frame = np.array([transform_lidar.location.x,transform_lidar.location.y,transform_lidar.location.z])
        trans_rot = np.zeros((4, 4))
        trans_rot[0,:] = np.array([math.cos(angle_lidar), math.sin(angle_lidar), 0.0, np.dot(-lidar_frame, np.array([math.cos(angle_lidar), math.sin(angle_lidar), 0.0]))])
        trans_rot[1,:] = np.array([-math.sin(angle_lidar), math.cos(angle_lidar), 0.0, np.dot(-lidar_frame, np.array([-math.sin(angle_lidar), math.cos(angle_lidar), 0.0]))]) 
        trans_rot[2,:] = np.array([0.0, 0.0, 1.0, np.dot(-lidar_frame,np.array([0.0, 0.0, 1.0]))])
        trans_rot[3,:] = np.array([0.0,0.0,0.0,1.0])
        translation = np.dot(trans_rot, np.array([transform.location.x,transform.location.y,transform.location.z,1]))
    else:
        translation = [0,0,0]
    eight_points = np.tile(translation[:3], (8,1))
	
    corner_box = np.dot(rotation_matrix, cords) + eight_points.transpose()
    """
    Doing transformation of location
    """
    reflect_to_x = np.array([[1,0,0],[0,-1,0],[0,0,1]])
    reflect_to_y = np.array([[-1,0,0],[0,1,0],[0,0,1]])
    rotate_180_z = np.array([[-1,0,0],[0,-1,0],[0,0,1]])
    reflect_to_xy = np.array([[0,1,0],[1,0,0],[0,0,1]])
    corner_box_new = (np.dot(reflect_to_y, corner_box)).transpose()
	
    return corner_box_new

def draw_bounding_boxes(bbox):
    """
    Draws bounding boxes on o3d display.
    """
    #"""
    # Our lines span from points 0 to 1, 1 to 2, 2 to 3, etc...
    lines = [[0, 1], [1, 2], [2, 3], [0, 3],
             [4, 5], [5, 6], [6, 7], [4, 7],
             [0, 4], [1, 5], [2, 6], [3, 7]]
    # Use the same color for all lines
    colors = [[1, 0, 0] for _ in range(len(lines))]
    line_set = o3d.geometry.LineSet()
    line_set.lines = o3d.utility.Vector2iVector(lines)
    line_set.colors = o3d.utility.Vector3dVector(colors)
	#"""
    # draw lines
    line_set.points = o3d.utility.Vector3dVector(bbox)
    """
    if frame==2:
        vis.add_geometry(line_set)
    vis.update_geometry(line_set)
    """
    return line_set
	
def get_bbox_json(vehicle, vehicle_lidar, bbox_old, process_time):
    """
    Returns 3D bounding box for a vehicle.
    """
    extent = vehicle.bounding_box.extent
    transform = vehicle.get_transform()
    transform_lidar = vehicle_lidar.get_transform()
    location = np.array(
	                    [transform.location.x - transform_lidar.location.x,
	                    transform.location.y - transform_lidar.location.y,
	                    transform.location.z - transform_lidar.location.z])
    angle_lidar = np.radians(transform_lidar.rotation.yaw)
    rotation = np.radians(transform.rotation.yaw)-np.radians(transform_lidar.rotation.yaw) 

    dist = math.sqrt(location[0]**2 + location[1]**2)
    if dist>0:
        lidar_frame = np.array([transform_lidar.location.x,transform_lidar.location.y,transform_lidar.location.z])
        trans_rot = np.zeros((4, 4))
        trans_rot[0,:] = np.array([math.cos(angle_lidar), math.sin(angle_lidar), 0.0, np.dot(-lidar_frame, np.array([math.cos(angle_lidar), math.sin(angle_lidar), 0.0]))])
        trans_rot[1,:] = np.array([-math.sin(angle_lidar), math.cos(angle_lidar), 0.0, np.dot(-lidar_frame, np.array([-math.sin(angle_lidar), math.cos(angle_lidar), 0.0]))]) 
        trans_rot[2,:] = np.array([0.0, 0.0, 1.0, np.dot(-lidar_frame,np.array([0.0, 0.0, 1.0]))])
        trans_rot[3,:] = np.array([0.0,0.0,0.0,1.0])
        translation = np.dot(trans_rot, np.array([transform.location.x,transform.location.y,transform.location.z,1]))
    else:
        translation = [0,0,0]
    
    """
    Doing transformation of location
    """
    reflect_to_x = np.array([[1,0,0],[0,-1,0],[0,0,1]])
    reflect_to_y = np.array([[-1,0,0],[0,1,0],[0,0,1]])
    translation_ref = np.dot(reflect_to_y, translation[:3])
    cx = translation_ref[0]
    cy = translation_ref[1]
    cz = translation_ref[2]
    dist = math.sqrt(cx**2 + cy**2 + cz**2)
    l = extent.x*2
    w = extent.y*2
    h = extent.z*2
    orient = rotation
    
    speed = math.sqrt(( cx - bbox_old['cx'])**2 + (cy - bbox_old['cy'])**2 + (cz - bbox_old['cz'])**2 )/ (process_time.total_seconds())

    json_string = np.array([cx,cy,cz,dist,l,w,h,orient,speed])
    return json_string
	
def get_bboxes(world, vehicle_lidar, bboxes_old, process_time):
    vehicles = world.get_actors().filter('vehicle.*')
    bboxes = {}
    i = 0
    for vehicle in vehicles:
        distance = dist(vehicle, vehicle_lidar)
        if (distance < 60):
            bboxes[i] = {}
            if (bboxes_old):
                bbox_old = bboxes_old[i]
            else:
                bbox_old = {'cx':0, 'cy':0, 'cz':0}

            bbox = get_bbox_json(vehicle, vehicle_lidar, bbox_old, process_time)

            bboxes[i]['cx'] = bbox[0]
            bboxes[i]['cy'] = bbox[1]
            bboxes[i]['cz'] = bbox[2]
            bboxes[i]['dist'] = bbox[3]
            bboxes[i]['l'] = bbox[4]
            bboxes[i]['w'] = bbox[5]
            bboxes[i]['h'] = bbox[6]
            bboxes[i]['orient'] = bbox[7]
            bboxes[i]['speed'] = bbox[8]
            i = i+1
    return bboxes

def dist(vehicle, vehicle_lidar):
    transform = vehicle.get_transform()
    transform_lidar = vehicle_lidar.get_transform()
    dist_x = transform.location.x - transform_lidar.location.x
    dist_y = transform.location.y - transform_lidar.location.y
    dist_z = transform.location.z - transform_lidar.location.z
    distance = math.sqrt(dist_x**2 + dist_y**2 + dist_z**2)	
    return distance

def detect_loop(world, frame, lidar, vehicle_lidar, vis, dt0):
    vehicles = world.get_actors().filter('vehicle.*')
    line_list = []
    for vehicle in vehicles:
        distance = dist(vehicle, vehicle_lidar)
        if (distance < 60):
            bounding_boxes = get_bounding_boxes(vehicle, vehicle_lidar)
            line_set = draw_bounding_boxes(bounding_boxes)
            #print("\n", get_bbox_json(vehicle, vehicle_lidar))
            line_list.append(line_set)
            #line_sets.lines = o3d.utility.Vector2iVector(line_list)
            #"""
            if frame%6 == 1:
                #vis.add_geometry(line_set)
                vis.clear_geometries()
            #time.sleep(0.005)
            vis.add_geometry(line_set)
			#"""

def main_code(arg): #, world, vehicle_lidar):
    """Main function of the script"""
    #"""
    client = carla.Client(arg.host, arg.port)
    client.set_timeout(5.0)
    world = client.get_world()
    #"""

    try:
        original_settings = world.get_settings()
        settings = world.get_settings()
        traffic_manager = client.get_trafficmanager(8000)
        traffic_manager.set_synchronous_mode(True)

        delta = 0.05

        settings.fixed_delta_seconds = delta
        settings.synchronous_mode = True
        settings.no_rendering_mode = arg.no_rendering
        world.apply_settings(settings)

        blueprint_library = world.get_blueprint_library()
        #"""		
        vehicle_bp = blueprint_library.filter(arg.filter)[0]
        vehicle_transform = random.choice(world.get_map().get_spawn_points())
        vehicle_lidar = world.spawn_actor(vehicle_bp, vehicle_transform)
        vehicle_lidar.set_autopilot(arg.no_autopilot)
        #"""

        lidar_bp = generate_lidar_bp(arg, world, blueprint_library, delta)

        user_offset = carla.Location(arg.x, arg.y, arg.z)
        lidar_transform = carla.Transform(carla.Location(x=-0.5, z=1.8) + user_offset)

        lidar = world.spawn_actor(lidar_bp, lidar_transform, attach_to=vehicle_lidar)
        #lidar.listen(lambda point_cloud: point_cloud.save_to_disk('coba/new_lidar_output/%.6d.ply' % point_cloud.frame))
        point_list = o3d.geometry.PointCloud()
        #line_sets = o3d.geometry.LineSet()
		
        if arg.semantic:
            lidar.listen(lambda data: semantic_lidar_callback(data, point_list))
        else:
            lidar.listen(lambda data: lidar_callback(data, point_list))

        vis = o3d.visualization.Visualizer()
        vis.create_window(
            window_name='Carla Lidar',
            width=960,
            height=540,
            left=480,
            top=270)
        vis.get_render_option().background_color = [0.05, 0.05, 0.05]
        vis.get_render_option().point_size = 1
        vis.get_render_option().show_coordinate_frame = True
        
        if arg.show_axis:
            add_open3d_axis(vis)

        frame = 0
        dt0 = datetime.now()
		
        while True:
            if frame == 2:
                vis.add_geometry(point_list)
            vis.update_geometry(point_list)
            """
            vehicles = world.get_actors().filter('vehicle.*')
            line_list = []
            for vehicle in vehicles:
                bbox_prop = get_bbox_json(vehicle, vehicle_lidar)
                distance = bbox_prop[3]
                if (distance < 60):
                    bounding_boxes = get_bounding_boxes(vehicle, vehicle_lidar)
                    line_set = draw_bounding_boxes(bounding_boxes)
                    #print("\n", get_bbox_json(vehicle, vehicle_lidar))
                    line_list.append(line_set)
                    #line_sets.lines = o3d.utility.Vector2iVector(line_list)
                    ""
                    if frame == 2:
                        vis.add_geometry(line_set)
                    #time.sleep(0.005)
                    vis.update_geometry(line_set)
                    #""
            #""
            for idx,line in enumerate(line_list, start=0):
                if frame == 2:
                    vis.add_geometry(line_list[idx])
                vis.add_geometry(line_list[idx])
            """
            detect_loop(world, frame, lidar, vehicle_lidar, vis, dt0)
            #if frame == 2:
                #vis.add_geometry(line_list)
                #o3d.visualization.draw_geometries(line_list)
            #vis.update_geometry(line_list)
            #o3d.visualization.draw_geometries(line_list)
            vis.poll_events()
            vis.update_renderer()
            # # This can fix Open3D jittering issues:
            time.sleep(0.005)
            #vis.remove_geometry(line_set)
            #vis.clear_geometries()

            #PRINT BBOX ARRAY
            bboxes = get_bboxes(world, vehicle_lidar)
            print(bboxes)
            world.tick()

            process_time = datetime.now() - dt0
            sys.stdout.write('\r' + 'FPS: ' + str(1.0 / process_time.total_seconds()))
            sys.stdout.flush()
            dt0 = datetime.now()
            frame += 1

    finally:
        world.apply_settings(original_settings)
        traffic_manager.set_synchronous_mode(False)

        vehicle_lidar.destroy()
        lidar.destroy()
        vis.destroy_window()