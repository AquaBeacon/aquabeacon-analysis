import cv2
import numpy as np
import pytest

from aquabeacon_analysis.camera_geometry import (
    display_geometry,
    resize_intrinsics_pixel_centers,
    rotate_intrinsics,
    transform_pixels,
)
from aquabeacon_analysis.ngt_position import calibrated_rays


@pytest.mark.parametrize('turns',[0,1,2,3])
def test_rotation_preserves_rays_and_distorted_projection(turns):
    k=np.array([[671.,0,646.2],[0,672.3,360.7],[0,0,1.]])
    d=np.array([.36,.35,-.006,.002,0.])
    points=np.array([[-.6,-.3,1],[.4,.3,1],[0,0,1],[.1,-.2,1.]])
    pixels=cv2.projectPoints(points,np.zeros(3),np.zeros(3),k,d)[0][:,0]
    kd,dd,r,size=rotate_intrinsics(k,d,(1280,720),turns)
    a,_,_=display_geometry((1280,720),turns)
    rotated=transform_pixels(pixels,a)
    projected=cv2.projectPoints(points@r.T,np.zeros(3),np.zeros(3),kd,dd)[0][:,0]
    np.testing.assert_allclose(rotated,projected,atol=1e-10)
    np.testing.assert_allclose(calibrated_rays(rotated,kd,dd),calibrated_rays(pixels,k,d)@r.T,atol=1e-10)
    assert size==((720,1280) if turns%2 else (1280,720))


def test_rotation_matches_actual_array_indices_and_extrinsic_chain():
    array=np.arange(15).reshape(3,5)
    a,r,size=display_geometry((5,3),1)
    for y,x in np.ndindex(array.shape):
        xd,yd=transform_pixels([x,y],a).astype(int)
        assert np.rot90(array,-1)[yd,xd]==array[y,x]
    sensor_to_imu=np.array([[0,0,1],[1,0,0],[0,1,0]])
    display_to_imu=sensor_to_imu@r.T
    ray=np.array([.2,.3,1])
    np.testing.assert_allclose(display_to_imu@(r@ray),sensor_to_imu@ray)
    assert size==(3,5)


def test_resize_and_rotation_commute_with_pixel_centers():
    k=np.array([[1007.,0,970.4],[0,1008.,540.6],[0,0,1.]])
    d=np.zeros(5)
    small=resize_intrinsics_pixel_centers(k,(1920,1080),(1280,720))
    first,_,_,_=rotate_intrinsics(small,d,(1280,720),2)
    full,_,_,_=rotate_intrinsics(k,d,(1920,1080),2)
    second=resize_intrinsics_pixel_centers(full,(1920,1080),(1280,720))
    np.testing.assert_allclose(first,second,atol=1e-12)
