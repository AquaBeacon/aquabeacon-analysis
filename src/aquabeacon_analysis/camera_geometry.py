"""Explicit pixel-center and optical-frame transforms for resized/rotated camera images."""
from __future__ import annotations

import numpy as np


def display_geometry(sensor_size, clockwise_quarters):
    """Return pixel homography, optical-axis rotation and displayed (width,height)."""
    w,h=map(int,sensor_size)
    if min(w,h)<=0 or clockwise_quarters not in range(4):
        raise ValueError('Positive sensor dimensions and 0..3 clockwise quarter turns required')
    a=np.eye(3); r=np.eye(3)
    quarter=np.array([[0.,-1,0],[1,0,0],[0,0,1]])
    for _ in range(clockwise_quarters):
        a=np.array([[0.,-1,h-1],[1,0,0],[0,0,1]])@a
        r=quarter@r; w,h=h,w
    return a,r,(w,h)


def rotate_intrinsics(matrix, distortion, sensor_size, clockwise_quarters):
    """Rotate OpenCV K and Brown tangential distortion into the displayed optical frame."""
    k=np.asarray(matrix,float); d=np.asarray(distortion,float).copy()
    if k.shape!=(3,3) or d.shape!=(5,) or abs(k[0,1])>1e-12:
        raise ValueError('Zero-skew 3x3 OpenCV K and five distortion coefficients required')
    a,r,size=display_geometry(sensor_size,clockwise_quarters)
    center=a@np.array([k[0,2],k[1,2],1.])
    fx,fy=(k[1,1],k[0,0]) if clockwise_quarters%2 else (k[0,0],k[1,1])
    result=np.array([[fx,0,center[0]],[0,fy,center[1]],[0,0,1.]])
    tangential=r[:2,:2]@np.array([d[3],d[2]])
    d[2],d[3]=tangential[1],tangential[0]
    return result,d,r,size


def resize_intrinsics_pixel_centers(matrix, source_size, target_size):
    """Full-frame pixel-center resize: x'=(x+0.5)*scale-0.5, preserving distortion."""
    k=np.asarray(matrix,float).copy()
    scales=np.asarray(target_size,float)/source_size
    if k.shape!=(3,3) or min(scales)<=0 or not np.isclose(scales[0],scales[1]):
        raise ValueError('Aspect-preserving full-frame resize required')
    k[:2]*=scales[:,None]
    k[:2,2]+=(scales-1)/2
    return k


def transform_pixels(pixels, homography):
    p=np.asarray(pixels,float)
    if p.shape[-1]!=2:
        raise ValueError('2D pixels required')
    return (np.concatenate([p,np.ones((*p.shape[:-1],1))],axis=-1)@np.asarray(homography).T)[...,:2]
