import numpy as np
import pytest

from aquabeacon_analysis.ngt_dense_patch import (
    consistent_disparity,
    grid_surface,
    independent_photo_support,
    relative_camera_pose,
    triangulate_rectified,
)


def test_known_stereo_depth_and_negative_disparity_rejection():
    p1=np.array([[100,0,50,0],[0,100,40,0],[0,0,1,0]],float)
    p2=p1.copy();p2[0,3]=-20
    xyz=triangulate_rectified([[60,40]],[10],p1,p2)
    np.testing.assert_allclose(xyz,[[.2,0,2]],atol=1e-10)
    p2=p1.copy();p2[1,3]=-20
    xyz=triangulate_rectified([[60,40]],[10],p1,p2,axis=1)
    np.testing.assert_allclose(xyz,[[.2,0,2]],atol=1e-10)
    with pytest.raises(ValueError):
        triangulate_rectified([[60,40]],[-1],p1,p2)


def test_actual_right_consistency_bounds_and_bad_masks():
    left=np.full((4,10),2.0);right=-left
    good=np.ones_like(left,dtype=bool)
    keep,_=consistent_disparity(left,right,good,good)
    assert not keep[:,:2].any() and keep[:,2:].all()
    vertical,_=consistent_disparity(left,right,good,good,axis=1)
    assert not vertical[:2].any() and vertical[2:].all()
    right[1,3]=-9
    keep,_=consistent_disparity(left,right,good,good)
    assert not keep[1,5]
    good[2,4]=False
    keep,_=consistent_disparity(left,right,good,np.ones_like(good))
    assert not keep[2,4]


def test_no_surface_over_gaps_or_depth_discontinuity():
    good=np.ones((4,4),bool);good[1,1]=False
    z=np.ones((4,4));z[:,3]=2
    faces=grid_surface(good,z)
    ids=np.full(good.shape,-1);ids[good]=np.arange(good.sum())
    assert len(faces)>0
    assert faces.max()<good.sum()
    # No face may cross the discontinuity into column 3.
    forbidden=set(ids[:,3])
    assert all(not (set(face)&forbidden) for face in faces)


def test_third_view_rejects_textureless_and_wrong_photo():
    rng=np.random.default_rng(42)
    image=rng.integers(0,255,(40,40),dtype=np.uint8)
    xy=np.array([[10,10],[20,20]])
    keep,ncc=independent_photo_support(image,image,xy,xy)
    assert keep.all() and (ncc>.99).all()
    keep,_=independent_photo_support(image,np.zeros_like(image),xy,xy)
    assert not keep.any()


def test_relative_camera_pose_convention():
    a={'position':[0,0,0],'quaternion_xyzw':[0,0,0,1]}
    b={'position':[.2,0,0],'quaternion_xyzw':[0,0,0,1]}
    r,t=relative_camera_pose(a,b)
    np.testing.assert_allclose(r,np.eye(3));np.testing.assert_allclose(t,[-.2,0,0])
