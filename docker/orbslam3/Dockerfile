FROM ubuntu:22.04

ARG ORB_SLAM3_COMMIT=4452a3c4ab75b1cde34e5505a36ec3f9edcdc4c4
ARG PANGOLIN_COMMIT=dd801d244db3a8e27b7fe8020cd751404aa818fd

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential cmake git pkg-config ca-certificates \
    libopencv-dev libeigen3-dev libglew-dev libboost-all-dev \
    libgl1-mesa-dev libglu1-mesa-dev libx11-dev libxi-dev libxrandr-dev \
    libavcodec-dev libavformat-dev libavutil-dev libswscale-dev \
    libpng-dev libjpeg-dev && rm -rf /var/lib/apt/lists/*

RUN git clone https://github.com/stevenlovegrove/Pangolin.git /opt/Pangolin \
    && cd /opt/Pangolin && git checkout "${PANGOLIN_COMMIT}" \
    && cmake -S . -B build -DCMAKE_BUILD_TYPE=Release \
       -DBUILD_TESTS=OFF -DBUILD_TOOLS=OFF -DBUILD_EXAMPLES=OFF \
       -DBUILD_PANGOLIN_VIDEO=OFF -DBUILD_PANGOLIN_PYTHON=OFF \
       -DBUILD_EXTERN_GLEW=OFF -DBUILD_EXTERN_LIBPNG=OFF -DBUILD_EXTERN_LIBJPEG=OFF \
    && cmake --build build --parallel 2 && cmake --install build

RUN apt-get update && apt-get install -y --no-install-recommends libssl-dev \
    && rm -rf /var/lib/apt/lists/*

COPY export_map_points.patch /tmp/export_map_points.patch
COPY export_tracked_features.patch /tmp/export_tracked_features.patch
COPY color_image_extension.patch /tmp/color_image_extension.patch

RUN git clone https://github.com/UZ-SLAMLab/ORB_SLAM3.git /opt/ORB_SLAM3 \
    && cd /opt/ORB_SLAM3 && git checkout "${ORB_SLAM3_COMMIT}" \
    && git apply /tmp/export_map_points.patch \
    && git apply /tmp/export_tracked_features.patch \
    && git apply /tmp/color_image_extension.patch \
    && sed -i 's/-march=native/-march=x86-64/g' CMakeLists.txt Thirdparty/g2o/CMakeLists.txt Thirdparty/DBoW2/CMakeLists.txt \
    && sed -i 's/IMU_MONOCULAR, true/IMU_MONOCULAR, false/' Examples/Monocular-Inertial/mono_inertial_euroc.cc \
    && cmake -S Thirdparty/DBoW2 -B Thirdparty/DBoW2/build -DCMAKE_BUILD_TYPE=Release \
    && cmake --build Thirdparty/DBoW2/build --parallel 2 \
    && cmake -S Thirdparty/g2o -B Thirdparty/g2o/build -DCMAKE_BUILD_TYPE=Release \
    && cmake --build Thirdparty/g2o/build --parallel 2 \
    && cmake -S Thirdparty/Sophus -B Thirdparty/Sophus/build -DCMAKE_BUILD_TYPE=Release \
    && cmake --build Thirdparty/Sophus/build --parallel 2 \
    && tar -xf Vocabulary/ORBvoc.txt.tar.gz -C Vocabulary \
    && cmake -S . -B build -DCMAKE_BUILD_TYPE=Release \
    && cmake --build build --target mono_inertial_euroc mono_euroc --parallel 2

ENV LD_LIBRARY_PATH=/opt/ORB_SLAM3/lib:/usr/local/lib
WORKDIR /work
CMD ["/opt/ORB_SLAM3/Examples/Monocular-Inertial/mono_inertial_euroc"]
