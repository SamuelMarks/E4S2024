import os, sys
import yaml
from scipy.spatial import ConvexHull
from glob import glob
import imageio
import numpy as np
from skimage.transform import resize
from skimage import img_as_ubyte
import torch
import torch.nn.functional as F

from e4s2024 import SHARE_PY_ROOT, DATASETS_ROOT
from swap_face_fine.face_vid2vid.sync_batchnorm import DataParallelWithCallback

from swap_face_fine.face_vid2vid.modules.generator import OcclusionAwareGenerator, OcclusionAwareSPADEGenerator
from swap_face_fine.face_vid2vid.modules.keypoint_detector import KPDetector, HEEstimator
from swap_face_fine.face_vid2vid.animate import normalize_kp


if sys.version_info[0] < 3:
    raise Exception("You must use Python 3 or higher. Recommended version is Python 3.7")

def load_checkpoints(config_path, checkpoint_path, gen, cpu=False):

    with open(config_path) as f:
        config = yaml.load(f, Loader=yaml.FullLoader)

    if gen == 'original':
        generator = OcclusionAwareGenerator(**config['model_params']['generator_params'],
                                            **config['model_params']['common_params'])
    elif gen == 'spade':
        generator = OcclusionAwareSPADEGenerator(**config['model_params']['generator_params'],
                                                 **config['model_params']['common_params'])

    if not cpu:
        generator.cuda()

    kp_detector = KPDetector(**config['model_params']['kp_detector_params'],
                             **config['model_params']['common_params'])
    if not cpu:
        kp_detector.cuda()

    he_estimator = HEEstimator(**config['model_params']['he_estimator_params'],
                               **config['model_params']['common_params'])
    if not cpu:
        he_estimator.cuda()
    
    if cpu:
        checkpoint = torch.load(checkpoint_path, map_location=torch.device('cpu'))
    else:
        checkpoint = torch.load(checkpoint_path)
 
    generator.load_state_dict(checkpoint['generator'])
    kp_detector.load_state_dict(checkpoint['kp_detector'])
    he_estimator.load_state_dict(checkpoint['he_estimator'])
    
    if not cpu:
        generator = DataParallelWithCallback(generator)
        kp_detector = DataParallelWithCallback(kp_detector)
        he_estimator = DataParallelWithCallback(he_estimator)

    generator.eval()
    kp_detector.eval()
    he_estimator.eval()
    
    return generator, kp_detector, he_estimator


def headpose_pred_to_degree(pred):
    device = pred.device
    idx_tensor = [idx for idx in range(66)]
    idx_tensor = torch.FloatTensor(idx_tensor).to(device)
    pred = F.softmax(pred)
    degree = torch.sum(pred*idx_tensor, axis=1) * 3 - 99

    return degree

'''
# beta version
def get_rotation_matrix(yaw, pitch, roll):
    yaw = yaw / 180 * 3.14
    pitch = pitch / 180 * 3.14
    roll = roll / 180 * 3.14

    roll = roll.unsqueeze(1)
    pitch = pitch.unsqueeze(1)
    yaw = yaw.unsqueeze(1)

    roll_mat = torch.cat([torch.ones_like(roll), torch.zeros_like(roll), torch.zeros_like(roll), 
                          torch.zeros_like(roll), torch.cos(roll), -torch.sin(roll),
                          torch.zeros_like(roll), torch.sin(roll), torch.cos(roll)], dim=1)
    roll_mat = roll_mat.view(roll_mat.shape[0], 3, 3)

    pitch_mat = torch.cat([torch.cos(pitch), torch.zeros_like(pitch), torch.sin(pitch), 
                           torch.zeros_like(pitch), torch.ones_like(pitch), torch.zeros_like(pitch),
                           -torch.sin(pitch), torch.zeros_like(pitch), torch.cos(pitch)], dim=1)
    pitch_mat = pitch_mat.view(pitch_mat.shape[0], 3, 3)

    yaw_mat = torch.cat([torch.cos(yaw), -torch.sin(yaw), torch.zeros_like(yaw),  
                         torch.sin(yaw), torch.cos(yaw), torch.zeros_like(yaw),
                         torch.zeros_like(yaw), torch.zeros_like(yaw), torch.ones_like(yaw)], dim=1)
    yaw_mat = yaw_mat.view(yaw_mat.shape[0], 3, 3)

    rot_mat = torch.einsum('bij,bjk,bkm->bim', roll_mat, pitch_mat, yaw_mat)

    return rot_mat

'''
def get_rotation_matrix(yaw, pitch, roll):
    yaw = yaw / 180 * 3.14
    pitch = pitch / 180 * 3.14
    roll = roll / 180 * 3.14

    roll = roll.unsqueeze(1)
    pitch = pitch.unsqueeze(1)
    yaw = yaw.unsqueeze(1)

    pitch_mat = torch.cat([torch.ones_like(pitch), torch.zeros_like(pitch), torch.zeros_like(pitch), 
                          torch.zeros_like(pitch), torch.cos(pitch), -torch.sin(pitch),
                          torch.zeros_like(pitch), torch.sin(pitch), torch.cos(pitch)], dim=1)
    pitch_mat = pitch_mat.view(pitch_mat.shape[0], 3, 3)

    yaw_mat = torch.cat([torch.cos(yaw), torch.zeros_like(yaw), torch.sin(yaw), 
                           torch.zeros_like(yaw), torch.ones_like(yaw), torch.zeros_like(yaw),
                           -torch.sin(yaw), torch.zeros_like(yaw), torch.cos(yaw)], dim=1)
    yaw_mat = yaw_mat.view(yaw_mat.shape[0], 3, 3)

    roll_mat = torch.cat([torch.cos(roll), -torch.sin(roll), torch.zeros_like(roll),  
                         torch.sin(roll), torch.cos(roll), torch.zeros_like(roll),
                         torch.zeros_like(roll), torch.zeros_like(roll), torch.ones_like(roll)], dim=1)
    roll_mat = roll_mat.view(roll_mat.shape[0], 3, 3)

    rot_mat = torch.einsum('bij,bjk,bkm->bim', pitch_mat, yaw_mat, roll_mat)

    return rot_mat

def keypoint_transformation(kp_canonical, he, estimate_jacobian=True, free_view=False, yaw=0, pitch=0, roll=0):
    kp = kp_canonical['value']
    if not free_view:
        yaw, pitch, roll = he['yaw'], he['pitch'], he['roll']
        yaw = headpose_pred_to_degree(yaw)
        pitch = headpose_pred_to_degree(pitch)
        roll = headpose_pred_to_degree(roll)
    else:
        if yaw is not None:
            yaw = torch.tensor([yaw]).cuda()
        else:
            yaw = he['yaw']
            yaw = headpose_pred_to_degree(yaw)
        if pitch is not None:
            pitch = torch.tensor([pitch]).cuda()
        else:
            pitch = he['pitch']
            pitch = headpose_pred_to_degree(pitch)
        if roll is not None:
            roll = torch.tensor([roll]).cuda()
        else:
            roll = he['roll']
            roll = headpose_pred_to_degree(roll)

    t, exp = he['t'], he['exp']

    rot_mat = get_rotation_matrix(yaw, pitch, roll)
    
    # keypoint rotation
    kp_rotated = torch.einsum('bmp,bkp->bkm', rot_mat, kp)

    # keypoint translation
    t = t.unsqueeze_(1).repeat(1, kp.shape[1], 1)
    kp_t = kp_rotated + t

    # add expression deviation 
    exp = exp.view(exp.shape[0], -1, 3)
    kp_transformed = kp_t + exp

    if estimate_jacobian:
        jacobian = kp_canonical['jacobian']
        jacobian_transformed = torch.einsum('bmp,bkps->bkms', rot_mat, jacobian)
    else:
        jacobian_transformed = None

    return {'value': kp_transformed, 'jacobian': jacobian_transformed}

def make_animation(source_image, driving_video, generator, kp_detector, he_estimator, relative=True, adapt_movement_scale=True, estimate_jacobian=True, cpu=False, free_view=False, yaw=0, pitch=0, roll=0):
    with torch.no_grad():
        predictions = []
        source = torch.tensor(source_image[np.newaxis].astype(np.float32)).permute(0, 3, 1, 2)
        if not cpu:
            source = source.cuda()
        driving = torch.tensor(np.array(driving_video)[np.newaxis].astype(np.float32)).permute(0, 4, 1, 2, 3)
        kp_canonical = kp_detector(source)
        he_source = he_estimator(source)
        he_driving_initial = he_estimator(driving[:, :, 0])

        kp_source = keypoint_transformation(kp_canonical, he_source, estimate_jacobian)
        kp_driving_initial = keypoint_transformation(kp_canonical, he_driving_initial, estimate_jacobian)
        # kp_driving_initial = keypoint_transformation(kp_canonical, he_driving_initial, free_view=free_view, yaw=yaw, pitch=pitch, roll=roll)

        for frame_idx in range(driving.shape[2]):
            driving_frame = driving[:, :, frame_idx]
            if not cpu:
                driving_frame = driving_frame.cuda()
            he_driving = he_estimator(driving_frame)
            kp_driving = keypoint_transformation(kp_canonical, he_driving, estimate_jacobian, free_view=free_view, yaw=yaw, pitch=pitch, roll=roll)
            # # 暂时不要用这个norm
            # kp_norm = normalize_kp(kp_source=kp_source, kp_driving=kp_driving,
            #                        kp_driving_initial=kp_driving_initial, use_relative_movement=relative,
            #                        use_relative_jacobian=estimate_jacobian, adapt_movement_scale=adapt_movement_scale)
            # out = generator(source, kp_source=kp_source, kp_driving=kp_norm)
            out = generator(source, kp_source=kp_source, kp_driving=kp_driving)

            predictions.append(np.transpose(out['prediction'].data.cpu().numpy(), [0, 2, 3, 1])[0])
    return predictions


# ================== 以下是自己 DIY 的 ============================
def init_facevid2vid_pretrained_model(cfg_path, ckpt_path):
    """
    实例化 预训练的 face_vid2vid 模型
    
    """
    generator, kp_detector, he_estimator = load_checkpoints(config_path=cfg_path, checkpoint_path=ckpt_path, gen="spade", cpu=False)

    with open(cfg_path) as f:
        config = yaml.load(f, Loader=yaml.FullLoader)
    estimate_jacobian = config['model_params']['common_params']['estimate_jacobian']
    # print(f'estimate jacobian: {estimate_jacobian}')
    
    print(f'Load face_vid2vid pre-trained model success!')
    return generator, kp_detector, he_estimator, estimate_jacobian


def drive_source_demo(
    source_im, target_ims,  # 输入图片相关参数
    generator, kp_detector, he_estimator, estimate_jacobian  # 模型相关参数
    ):
    """ 
    驱动 source image
    
    args:
        source_im (np.array): [H,W,3] 256*256 大小, [0,1]范围
        target_ims (List[np.array]): List 中每个图片的格式为[H,W,3] 256*256 大小, [0,1]范围
    return:
    
        predictions (List[np.array]): 驱动后的结果, List 中每个图片的格式为[H,W,3] 256*256 大小, [0,1]范围 
    """
    
    predictions = make_animation(source_im, target_ims, generator, kp_detector, he_estimator, 
                                 relative=True, adapt_movement_scale=True, estimate_jacobian=estimate_jacobian)
        
    return predictions

if __name__ == "__main__":
    
    generator, kp_detector, he_estimator, estimate_jacobian = init_pretrained_model("{}/One-Shot_Free-View_Neural_Talking_Head_Synthesis/config/vox-256.yaml".format(SHARE_PY_ROOT),
                                                                                    "{}/One-Shot_Free-View_Neural_Talking_Head_Synthesis/ckpts/00000189-checkpoint.pth.tar".format(SHARE_PY_ROOT))
    
    base_dir = "{}/our_swapping_dataset/driven_images_256/epoch_00190_iteration_000400000".format(DATASETS_ROOT)
    source_img_names = ["28139","28260","28398" , "28494" , "28556" , "28618",  "28622" , "28688"  ,"28831",  "28837" , "28965", "29357","29376","29441","29575","29581","29927","29980"]
    for source_name in source_img_names:
        source_image = imageio.imread(os.path.join("{}/CelebA-HQ/test/images","%s.jpg".format(DATASETS_ROOT)%source_name))
        
        target_names = sorted(glob(os.path.join(base_dir, source_name, "%s_to_*.png"%source_name)))
        target_names =  [name[-9:-4] for name in target_names]

        driving_images = [imageio.imread(os.path.join("{}/CelebA-HQ/test/images".format(DATASETS_ROOT),"%s.jpg"%target_name)) for target_name in target_names]
            
        source_image = resize(source_image, (256, 256))[..., :3]
        driving_images = [resize(frame, (256, 256))[..., :3] for frame in driving_images]
        
        # 跑模型
        predictions = drive_source_demo(source_image, driving_images, 
                                        generator, kp_detector, he_estimator, estimate_jacobian)
        
        for idx, frame in enumerate(predictions):
            pred = img_as_ubyte(frame)
            imageio.imsave(
                os.path.join("result_dir","%s_to_%s.png"%(os.path.basename(source_name).split('.')[0],target_names[idx])),
                pred
            )
