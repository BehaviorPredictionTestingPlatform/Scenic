"""Simulator interface for CARLA."""

try:
	import carla
except ImportError as e:
	raise ModuleNotFoundError('CARLA scenarios require the "carla" Python package') from e

import json
import math
import numpy as np
import os

from scenic.syntax.translator import verbosity
if verbosity == 0:	# suppress pygame advertisement at zero verbosity
	os.environ['PYGAME_HIDE_SUPPORT_PROMPT'] = 'hide'
import pygame

from carla import ColorConverter as cc

from scenic.domains.driving.simulators import DrivingSimulator, DrivingSimulation
from scenic.core.simulators import SimulationCreationError
from scenic.syntax.veneer import verbosePrint
import scenic.simulators.carla.utils.utils as utils
import scenic.simulators.carla.utils.visuals as visuals
import scenic.simulators.carla.utils.recording_utils as rec_utils
from scenic.simulators.carla.blueprints import *

class CarlaSimulator(DrivingSimulator):
	"""Implementation of `Simulator` for CARLA."""
	def __init__(self, carla_map, map_path, address='127.0.0.1', port=2000, timeout=10,
				 render=True, record=False, timestep=0.1, traffic_manager_port=None):
		super().__init__()
		verbosePrint('Connecting to CARLA on port {port}')
		self.client = carla.Client(address, port)
		self.client.set_timeout(timeout)  # limits networking operations (seconds)
		if carla_map is not None:
			self.world = self.client.load_world(carla_map)
		else:
			if map_path.endswith('.xodr'):
				with open(map_path) as odr_file:
					self.world = self.client.generate_opendrive_world(odr_file.read())
			else:
				raise RuntimeError(f'CARLA only supports OpenDrive maps')
		self.timestep = timestep

		if traffic_manager_port is None:
			traffic_manager_port = port + 6000
		self.tm = self.client.get_trafficmanager(traffic_manager_port)
		self.tm.set_synchronous_mode(True)

		# Set to synchronous with fixed timestep
		settings = self.world.get_settings()
		settings.synchronous_mode = True
		settings.fixed_delta_seconds = timestep  # NOTE: Should not exceed 0.1
		self.world.apply_settings(settings)
		verbosePrint('Map loaded in simulator.')

		self.render = render  # visualization mode ON/OFF
		self.record = record  # whether to use the carla recorder
		self.scenario_number = 0  # Number of the scenario executed

	def createSimulation(self, scene, verbosity=0, sensor_config=None):
		self.scenario_number += 1
		return CarlaSimulation(scene, self.client, self.tm, self.timestep,
							   render=self.render, record=self.record,
							   scenario_number=self.scenario_number, sensor_config=sensor_config,
							   verbosity=verbosity)

	def toggle_recording(self, record):
		self.record = record

	def is_recording(self):
		return self.record

	def destroy(self):
		settings = self.world.get_settings()
		settings.synchronous_mode = False
		settings.fixed_delta_seconds = None
		self.world.apply_settings(settings)
		self.tm.set_synchronous_mode(False)

		super().destroy()


class CarlaSimulation(DrivingSimulation):
	def __init__(self, scene, client, tm, timestep, render, record, scenario_number, sensor_config=None, verbosity=0):
		super().__init__(scene, timestep=timestep, verbosity=verbosity)
		self.client = client
		self.world = self.client.get_world()
		self.map = self.world.get_map()
		self.blueprintLib = self.world.get_blueprint_library()
		self.tm = tm
		
		weather = scene.params.get("weather")
		if weather is not None:
			if isinstance(weather, str):
				self.world.set_weather(getattr(carla.WeatherParameters, weather))
			elif isinstance(weather, dict):
				self.world.set_weather(carla.WeatherParameters(**weather))

		# Reloads current world: destroys all actors, except traffic manager instances
		# self.client.reload_world()

		# Setup HUD
		self.render = render
		self.record = record
		self.scenario_number = scenario_number
		if self.render:
			self.displayDim = (1280, 720)
			self.displayClock = pygame.time.Clock()
			self.camTransform = 0
			pygame.init()
			pygame.font.init()
			self.hud = visuals.HUD(*self.displayDim)
			self.display = pygame.display.set_mode(
				self.displayDim,
				pygame.HWSURFACE | pygame.DOUBLEBUF
			)
			self.cameraManager = None

		self.rgb_frame_buffer = []
		self.semantic_frame_buffer = []
		self.lidar_data_buffer = []
		self.bbox_buffer = []

		# if self.record:
		# 	if not os.path.exists(self.record):
		# 		os.mkdir(self.record)
		# 	name = "{}/scenario{}.log".format(self.record, self.scenario_number)
		# 	self.client.start_recorder(name)

		# Create Carla actors corresponding to Scenic objects
		self.ego = None
		for obj in self.objects:
			carlaActor = self.createObjectInSimulator(obj)

			# Check if ego (from carla_scenic_taks.py)
			if obj is self.objects[0]:
				self.ego = obj

				# Set up camera manager and collision sensor for ego
				if self.render:
					camIndex = 0
					camPosIndex = 0
					self.cameraManager = visuals.CameraManager(self.world, carlaActor, self.hud)
					self.cameraManager._transform_index = camPosIndex
					self.cameraManager.set_sensor(camIndex)
					self.cameraManager.set_transform(self.camTransform)
					self.cameraManager._recording = self.record

					if self.record:
						if sensor_config is None:
							raise RuntimeError('Must specify sensor configuraiton file when recording.')

						with open(sensor_config, 'r') as f:
							sensors = json.load(f)

						self.sensors = []

						for sensor in sensors:
							t_x, t_y, t_z = sensor['transform']['location']
							loc = carla.Location(x=t_x, y=t_y, z=t_z)
							rot = carla.Rotation()
							if 'rotation' in sensor['transform']:
								yaw, ptich, roll = sensor['transform']['rotation']
								rot = carla.Rotation(yaw=yaw, pitch=pitch, roll=roll)
							sensor_transform = carla.Transform(loc, rot)

							if sensor['type'] == 'rgb':
								VIEW_WIDTH = sensor['settings']['VIEW_WIDTH']
								VIEW_HEIGHT = sensor['settings']['VIEW_HEIGHT']
								VIEW_FOV = sensor['settings']['VIEW_FOV']

								sensor_dict = {
									'name': sensor['name'],
									'type': sensor['type'],
									'rgb_buffer': []
								}
								self.sensors.append(sensor_dict)

								bp = self.world.get_blueprint_library().find('sensor.camera.rgb')
								bp.set_attribute('image_size_x', str(VIEW_WIDTH))
								bp.set_attribute('image_size_y', str(VIEW_HEIGHT))
								bp.set_attribute('fov', str(VIEW_FOV))
								rgb_cam = self.world.spawn_actor(bp, sensor_transform, attach_to=carlaActor)
								rgb_buffer = sensor_dict['rgb_buffer']
								rgb_cam.listen(lambda x: self.process_rgb_image(x, rgb_buffer))
								sensor_dict['rgb_cam_obj'] = rgb_cam
							
							elif sensor['type'] == 'depth':
								VIEW_WIDTH = sensor['settings']['VIEW_WIDTH']
								VIEW_HEIGHT = sensor['settings']['VIEW_HEIGHT']
								VIEW_FOV = sensor['settings']['VIEW_FOV']

								sensor_dict = {
									'name': sensor['name'],
									'type': sensor['type'],
									'depth_buffer': []
								}
								self.sensors.append(sensor_dict)

								bp = self.world.get_blueprint_library().find('sensor.camera.depth')
								bp.set_attribute('image_size_x', str(VIEW_WIDTH))
								bp.set_attribute('image_size_y', str(VIEW_HEIGHT))
								bp.set_attribute('fov', str(VIEW_FOV))
								depth_cam = self.world.spawn_actor(bp, sensor_transform, attach_to=carlaActor)
								depth_buffer = sensor_dict['depth_buffer']
								depth_cam.listen(lambda x: self.process_depth_image(x, depth_buffer))
								sensor_dict['depth_cam_obj'] = depth_cam
							
							elif sensor['type'] == 'semantic_segmentation':
								VIEW_WIDTH = sensor['settings']['VIEW_WIDTH']
								VIEW_HEIGHT = sensor['settings']['VIEW_HEIGHT']
								VIEW_FOV = sensor['settings']['VIEW_FOV']

								sensor_dict = {
									'name': sensor['name'],
									'type': sensor['type'],
									'semantic_buffer': []
								}
								self.sensors.append(sensor_dict)

								bp = self.world.get_blueprint_library().find('sensor.camera.semantic_segmentation')
								bp.set_attribute('image_size_x', str(VIEW_WIDTH))
								bp.set_attribute('image_size_y', str(VIEW_HEIGHT))
								semantic_cam = self.world.spawn_actor(bp, sensor_transform, attach_to=carlaActor)
								semantic_buffer = sensor_dict['semantic_buffer']
								semantic_cam.listen(lambda x: self.process_semantic_image(x, semantic_buffer))
								sensor_dict['semantic_cam_obj'] = semantic_cam

							elif sensor['type'] == 'lidar':
								sensor_dict = {
									'name': sensor['name'],
									'type': sensor['type'],
									'lidar_buffer': []
								}
								self.sensors.append(sensor_dict)

								POINTS_PER_SECOND = sensor['settings']['PPS']
								UPPER_FOV = sensor['settings']['UPPER_FOV']
								LOWER_FOV = sensor['settings']['LOWER_FOV']
								RANGE = sensor['settings']['RANGE']
								ROTATION_FREQUENCY = sensor['settings']['ROTATION_FREQUENCY']

								bp = self.world.get_blueprint_library().find('sensor.lidar.ray_cast_semantic')
								bp.set_attribute('points_per_second', str(POINTS_PER_SECOND))
								bp.set_attribute('upper_fov', str(UPPER_FOV))
								bp.set_attribute('lower_fov', str(LOWER_FOV))
								bp.set_attribute('range', str(RANGE))
								bp.set_attribute('rotation_frequency', str(ROTATION_FREQUENCY))
								lidar_sensor = self.world.spawn_actor(bp, sensor_transform, attach_to=carlaActor)
								lidar_sensor.listen(lambda x: self.process_lidar_data(x, sensor_dict['lidar_buffer']))
								sensor_dict['lidar_obj'] = lidar_sensor

							elif sensor['type'] == 'radar':
								sensor_dict = {
									'name': sensor['name'],
									'type': sensor['type'],
									'radar_buffer': []
								}
								self.sensors.append(sensor_dict)

								HORIZONTAL_FOV = sensor['settings']['HORIZONTAL_FOV']
								VERTICAL_FOV = sensor['settings']['VERTICAL_FOV']
								POINTS_PER_SECOND = sensor['settings']['PPS']
								RANGE = sensor['settings']['RANGE']

								bp = self.world.get_blueprint_library().find('sensor.other.radar')
								bp.set_attribute('horizontal_fov', str(HORIZONTAL_FOV))
								bp.set_attribute('vertical_fov', str(VERTICAL_FOV))
								bp.set_attribute('points_per_second', str(POINTS_PER_SECOND))
								bp.set_attribute('range', str(RANGE))
								radar_sensor = self.world.spawn_actor(bp, sensor_transform, attach_to=carlaActor)
								radar_sensor.listen(lambda x: self.process_radar_data(x, sensor_dict['radar_buffer']))
								sensor_dict['radar_obj'] = radar_sensor

		self.world.tick() ## allowing manualgearshift to take effect 	# TODO still need this?

		for obj in self.objects:
			if isinstance(obj.carlaActor, carla.Vehicle):
				obj.carlaActor.apply_control(carla.VehicleControl(manual_gear_shift=False))

		self.world.tick()

		# Set Carla actor's initial speed (if specified)
		for obj in self.objects:
			if obj.speed is not None:
				equivVel = utils.scenicSpeedToCarlaVelocity(obj.speed, obj.heading)
				if hasattr(obj.carlaActor, 'set_target_velocity'):
					obj.carlaActor.set_target_velocity(equivVel)
				else:
					obj.carlaActor.set_velocity(equivVel)

	def createObjectInSimulator(self, obj):
		# Extract blueprint
		blueprint = self.blueprintLib.find(obj.blueprint)
		if obj.rolename is not None:
			blueprint.set_attribute('role_name', obj.rolename)

		# set walker as not invincible
		if blueprint.has_attribute('is_invincible'):
			blueprint.set_attribute('is_invincible', 'False')

		# Set up transform
		loc = utils.scenicToCarlaLocation(obj.position, world=self.world, blueprint=obj.blueprint)
		rot = utils.scenicToCarlaRotation(obj.heading)
		transform = carla.Transform(loc, rot)

		# Create Carla actor
		carlaActor = self.world.try_spawn_actor(blueprint, transform)
		if carlaActor is None:
			self.destroy()
			raise SimulationCreationError(f'Unable to spawn object {obj}')
		obj.carlaActor = carlaActor

		carlaActor.set_simulate_physics(obj.physics)

		if isinstance(carlaActor, carla.Vehicle):
			carlaActor.apply_control(carla.VehicleControl(manual_gear_shift=True, gear=1))
		elif isinstance(carlaActor, carla.Walker):
			carlaActor.apply_control(carla.WalkerControl())
			# spawn walker controller
			controller_bp = self.blueprintLib.find('controller.ai.walker')
			controller = self.world.try_spawn_actor(controller_bp, carla.Transform(), carlaActor)
			if controller is None:
				self.destroy()
				raise SimulationCreationError(f'Unable to spawn carla controller for object {obj}')
			obj.carlaController = controller
		return carlaActor

	def executeActions(self, allActions):
		super().executeActions(allActions)

		# Apply control updates which were accumulated while executing the actions
		for obj in self.agents:
			ctrl = obj._control
			if ctrl is not None:
				obj.carlaActor.apply_control(ctrl)
				obj._control = None

	def step(self):
		# Run simulation for one timestep
		self.world.tick()

		if self.record:
			vehicles = self.world.get_actors().filter('vehicle.*')

			curr_frame_idx = self.world.get_snapshot().frame

			bounding_boxes_3d = rec_utils.BBoxUtil.get_bounding_boxes(vehicles, self.rgb_cam)
			bounding_boxes_2d = rec_utils.BBoxUtil.get_2d_bounding_boxes(bounding_boxes_3d)
			self.bbox_buffer.append((curr_frame_idx, bounding_boxes_2d))

		# Render simulation
		if self.render:
			# self.hud.tick(self.world, self.ego, self.displayClock)
			self.cameraManager.render(self.display)
			# self.hud.render(self.display)
			pygame.display.flip()

	def getProperties(self, obj, properties):
		# Extract Carla properties
		carlaActor = obj.carlaActor
		currTransform = carlaActor.get_transform()
		currLoc = currTransform.location
		currRot = currTransform.rotation
		currVel = carlaActor.get_velocity()
		currAngVel = carlaActor.get_angular_velocity()

		# Prepare Scenic object properties
		velocity = utils.carlaToScenicPosition(currVel)
		speed = math.hypot(*velocity)

		values = dict(
			position=utils.carlaToScenicPosition(currLoc),
			elevation=utils.carlaToScenicElevation(currLoc),
			heading=utils.carlaToScenicHeading(currRot),
			velocity=velocity,
			speed=speed,
			angularSpeed=utils.carlaToScenicAngularSpeed(currAngVel),
		)
		return values

	def currentState(self):
		return tuple((obj.position.x, obj.position.y, obj.heading) for obj in self.objects)

	def process_rgb_image(self, image, buffer):
		image.convert(cc.Raw)
		buffer.append(image)

	def process_depth_image(self, image, buffer):
		image.convert(cc.LogarithmicDepth)
		buffer.append(image)

	def process_semantic_image(self, image, buffer):
		image.convert(cc.CityScapesPalette)
		buffer.append(image)

	def process_lidar_data(self, lidar_data, buffer):
		buffer.append(lidar_data)

	def process_radar_data(self, radar_data, buffer):
		buffer.append(radar_data)

	def save_recordings(self, save_dir):
		if not self.record:
			print('No recordings saved; turn on recordings for simulator to enable')
			return

		print('Started saving recorded data')

		if not os.path.isdir(save_dir):
			os.mkdir(save_dir)

		# Find frame indices for which all sensors have data (so that recordings are synchronized)
		common_frame_idxes = None
		for sensor in self.sensors:
			frame_idxes = {}

			if sensor['type'] == 'rgb':
				frame_idxes = {data.frame for data in sensor['rgb_buffer']}
				frame_idxes = frame_idxes.intersection({data.frame for data in sensor['depth_buffer']})
				frame_idxes = frame_idxes.intersection({data.frame for data in sensor['semantic_buffer']})
			elif sensor['type'] == 'lidar':
				frame_idxes = {data.frame for data in sensor['lidar_buffer']}
			elif sensor['type'] == 'radar':
				frame_idxes = {data.frame for data in sensor['radar_buffer']}

			if common_frame_idxes is None:
				common_frame_idxes = frame_idxes
			else:
				common_frame_idxes = common_frame_idxes.intersection(frame_idxes)

		# Intersect with bounding box frame indices
		common_frame_idxes = common_frame_idxes.intersection({frame_idx for frame_idx, _ in self.bbox_buffer})

		common_frame_idxes = sorted(list(common_frame_idxes))

		for sensor in self.sensors:
			if sensor['type'] == 'rgb':
				rgb_recording = rec_utils.VideoRecording()
				rgb_data = {data.frame: data for data in sensor['rgb_buffer']}

				for frame_idx in common_frame_idxes:
					rgb_recording.add_frame(rgb_data[frame_idx])

				sensor_dir = os.path.join(save_dir, sensor['name'])
				if not os.path.isdir(sensor_dir):
					os.mkdir(sensor_dir)
				rgb_filepath = os.path.join(sensor_dir, 'rgb.mp4')
				rgb_recording.save(rgb_filepath)

			elif sensor['type'] == 'depth':
				depth_recording = rec_utils.VideoRecording()
				depth_data = {data.frame: data for data in sensor['depth_buffer']}

				for frame_idx in common_frame_idxes:
					depth_recording.add_frame(depth_data[frame_idx])

				sensor_dir = os.path.join(save_dir, sensor['name'])
				if not os.path.isdir(sensor_dir):
					os.mkdir(sensor_dir)
				depth_filepath = os.path.join(sensor_dir, 'depth.mp4')
				depth_recording.save(depth_filepath)

			elif sensor['type'] == 'semantic_segmentation':
				semantic_recording = rec_utils.VideoRecording()
				semantic_data = {data.frame: data for data in sensor['semantic_buffer']}

				for frame_idx in common_frame_idxes:
					semantic_recording.add_frame(semantic_data[frame_idx])

				sensor_dir = os.path.join(save_dir, sensor['name'])
				if not os.path.isdir(sensor_dir):
					os.mkdir(sensor_dir)
				semantic_filepath = os.path.join(sensor_dir, 'semantic.mp4')
				semantic_recording.save(semantic_filepath)

			elif sensor['type'] == 'lidar':
				lidar_recording = rec_utils.FrameRecording()
				lidar_data = {data.frame: data for data in sensor['lidar_buffer']}

				for frame_idx in common_frame_idxes:
					classified_lidar_points = [[i.point.x, i.point.y, i.point.z, i.object_tag] for i in lidar_data[frame_idx]]
					lidar_recording.add_frame(classified_lidar_points)

				sensor_dir = os.path.join(save_dir, sensor['name'])
				if not os.path.isdir(sensor_dir):
					os.mkdir(sensor_dir)
				lidar_filepath = os.path.join(sensor_dir, 'lidar.json')
				lidar_recording.save(lidar_filepath)

			elif sensor['type'] == 'radar':
				radar_recording = rec_utils.FrameRecording()
				radar_data = {data.frame: data for data in sensor['radar_buffer']}

		# Save bounding boxes
		bbox_dir = os.path.join(save_dir, 'annotations')
		if not os.path.isdir(bbox_dir):
			os.mkdir(bbox_dir)

		bbox_recording = rec_utils.BBoxRecording()

		bbox_data = {frame_idx: bboxes for frame_idx, bboxes in self.bbox_buffer}

		for frame_idx in common_frame_idxes:
			bbox_recording.add_frame(bbox_data[frame_idx])

		bbox_filepath = os.path.join(bbox_dir, 'bboxes.json')
		bbox_recording.save(bbox_filepath)

	def destroy(self):
		for obj in self.objects:
			if obj.carlaActor is not None:
				if isinstance(obj.carlaActor, carla.Vehicle):
					obj.carlaActor.set_autopilot(False, self.tm.get_port())
				if isinstance(obj.carlaActor, carla.Walker):
					obj.carlaController.stop()
					obj.carlaController.destroy()
				obj.carlaActor.destroy()
		if self.render and self.cameraManager:
			self.cameraManager.destroy_sensor()

		self.client.stop_recorder()

		self.world.tick()
		super().destroy()
