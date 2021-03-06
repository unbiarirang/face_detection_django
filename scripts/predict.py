from ...utils.im_transform import imcv2_recolor, imcv2_affine_trans
from ...utils.box import BoundBox, box_iou, prob_compare
import numpy as np
import cv2
import os
import json
from django.utils import timezone
from detection.models import Record
from ...cython_utils.cy_yolo_findboxes import yolo_box_constructor

def _fix(obj, dims, scale, offs):
	for i in range(1, 5):
		dim = dims[(i + 1) % 2]
		off = offs[(i + 1) % 2]
		obj[i] = int(obj[i] * scale - off)
		obj[i] = max(min(obj[i], dim), 0)

def resize_input(self, im):
	h, w, c = self.meta['inp_size']
	imsz = cv2.resize(im, (w, h))
	imsz = imsz / 255.
	imsz = imsz[:,:,::-1]
	return imsz

def process_box(self, b, h, w, threshold):
	max_indx = np.argmax(b.probs)
	max_prob = b.probs[max_indx]
	label = self.meta['labels'][max_indx]
	if max_prob > threshold:
		left  = int ((b.x - b.w/2.) * w)
		right = int ((b.x + b.w/2.) * w)
		top   = int ((b.y - b.h/2.) * h)
		bot   = int ((b.y + b.h/2.) * h)
		if left  < 0    :  left = 0
		if right > w - 1: right = w - 1
		if top   < 0    :   top = 0
		if bot   > h - 1:   bot = h - 1
		mess = '{}'.format(label)
		return (left, right, top, bot, mess, max_indx, max_prob)
	return None

def findboxes(self, net_out):
	meta, FLAGS = self.meta, self.FLAGS
	threshold = FLAGS.threshold
	
	boxes = []
	boxes = yolo_box_constructor(meta, net_out, threshold)
	
	return boxes

def preprocess(self, im, allobj = None):
	"""
	Takes an image, return it as a numpy tensor that is readily
	to be fed into tfnet. If there is an accompanied annotation (allobj),
	meaning this preprocessing is serving the train process, then this
	image will be transformed with random noise to augment training data,
	using scale, translation, flipping and recolor. The accompanied
	parsed annotation (allobj) will also be modified accordingly.
	"""
	if type(im) is not np.ndarray:
		im = cv2.imread(im)

	if allobj is not None: # in training mode
		result = imcv2_affine_trans(im)
		im, dims, trans_param = result
		scale, offs, flip = trans_param
		for obj in allobj:
			_fix(obj, dims, scale, offs)
			if not flip: continue
			obj_1_ =  obj[1]
			obj[1] = dims[0] - obj[3]
			obj[3] = dims[0] - obj_1_
		im = imcv2_recolor(im)

	im = self.resize_input(im)
	if allobj is None: return im
	return im#, np.array(im) # for unit testing

person_img_prev = None
MATCH_THRESHOLD = 20
countframe = 0
flag = 0
def postprocess(self, net_out, im, t1, t2, phase, save = True,):
	"""
	Takes net output, draw predictions, save to disk
	"""
	global countframe
	global flag
	meta, FLAGS = self.meta, self.FLAGS
	threshold = FLAGS.threshold
	colors, labels = meta['colors'], meta['labels']

	boxes = self.findboxes(net_out)

	if type(im) is not np.ndarray:
		imgcv = cv2.imread(im)
	else: imgcv = im

	# draw bounding box
	h, w, _ = imgcv.shape
	resultsForJSON = []
	self.FLAGS.json = True
	for b in boxes:
		boxResults = self.process_box(b, h, w, threshold)
		if boxResults is None:
			continue
		left, right, top, bot, mess, max_indx, confidence = boxResults
		thick = int((h + w) // 300)
		if self.FLAGS.json:
			resultsForJSON.append({"label": mess, "confidence": float('%.2f' % confidence), "topleft": {"x": left, "y": top}, "bottomright": {"x": right, "y": bot}})
			# continue

		cv2.rectangle(imgcv,
			(left, top), (right, bot),
			self.meta['colors'][max_indx], thick)
		cv2.putText(
			imgcv, mess, (left, top - 12),
			0, 1e-3 * h, self.meta['colors'][max_indx],
			thick // 3)
		if phase == 2 or (countframe > int(t1) and countframe < int(t2) and int(t2) >= 0):
			cv2.rectangle(imgcv,
				(0, 0), (800, 60),
				(0,255,255), -1)
			cv2.rectangle(imgcv,
				(0, 420), (800, 480),
				(0,255,255), -1)
			cv2.putText(
				imgcv,"WARNING", (210, 45),
				2,1.5, (0,0,0),
				thick)
			if phase != 2 and flag != 2:
				Record.objects.create(phase='phase1',type='time limit exceeded',date=timezone.now())
				flag = 2
		if phase == 3 or (countframe >= int(t2) and int(t2) >= 0):
			cv2.rectangle(imgcv,
				(0, 0), (800, 60),
				(0,0,219), -1)
			cv2.rectangle(imgcv,
				(0, 420), (800, 480),
				(0,0,219), -1)
			cv2.putText(
				imgcv,"WARNING", (210, 45),
				2,1.5, (0,0,0),
				thick)
			if phase != 3 and flag != 3:
				flag = 3
				Record.objects.create(phase='phase2',type='time limit exceeded',date=timezone.now())
			
		# compute similarity score, check whether a certain person is staying or not
		if mess is "person":
			person_img = imgcv[top : bot, left : right]
			global person_img_prev
			if person_img_prev is None:
				person_img_prev = person_img

			orb = cv2.ORB_create()
			kp1, des1 = orb.detectAndCompute(person_img, None)
			kp2, des2 = orb.detectAndCompute(person_img_prev, None)

			if des1 is not None and des2 is not None:
				bf = cv2.BFMatcher(cv2.NORM_HAMMING)
				matches = bf.knnMatch(des1, des2, k = 2)

				good = []
				matches = [match for match in matches if len(match) == 2]
				for m, n in matches:
					if m.distance < 0.75 * n.distance:
						good.append([m])
				print("got " + str(len(good)) + " good matches")
				person_img_prev = person_img

				if len(good) >= MATCH_THRESHOLD:
					countframe = countframe + 1
				else:
					countframe = 0
					flag = 0
					


	print(json.dumps(resultsForJSON))
	if not save:
		return imgcv, resultsForJSON


	outfolder = os.path.join(self.FLAGS.imgdir, 'out')
	img_name = os.path.join(outfolder, 'results')#os.path.basename(im))
	if self.FLAGS.json:
		textJSON = json.dumps(resultsForJSON)
		textFile = img_name+ ".txt"
		with open(textFile, 'a+') as f:
			f.write(textJSON)
			f.write('\n')
		return imgcv

	cv2.imwrite(img_name, imgcv)