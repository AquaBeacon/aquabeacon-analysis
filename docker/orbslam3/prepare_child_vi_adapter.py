"""Instrument the installed ORB-SLAM3 adapter; leave estimator/library unchanged."""
from pathlib import Path

source = Path('/input/mono_euroc_empty_safe.cc').read_text()
source = source.replace('#include<MapPoint.h>', '#include<MapPoint.h>\n#include<KeyFrame.h>')
source = source.replace('ORB_SLAM3::System::MONOCULAR, false',
                        'ORB_SLAM3::System::IMU_MONOCULAR, false')
source = source.replace('associated_features\\n',
                        'associated_features,map_id,imu_initialized,imu_samples\\n')
source = source.replace('        // Main loop', r'''
        vector<ORB_SLAM3::IMU::Point> imu;
        ifstream input(string(argv[(2*seq)+3]) + "/mav0/imu0/data.csv");
        string line;
        while (getline(input, line)) {
            if (line.empty() || line[0] == '#') continue;
            replace(line.begin(), line.end(), ',', ' ');
            stringstream stream(line);
            double stamp; float gx,gy,gz,ax,ay,az;
            if (stream >> stamp >> gx >> gy >> gz >> ax >> ay >> az)
                imu.emplace_back(ax,ay,az,gx,gy,gz,stamp*1e-9);
        }
        if (imu.empty()) { cerr << "Missing IMU samples" << endl; return 2; }
        size_t nextImu = 0;
        while (nextImu+1 < imu.size() && imu[nextImu+1].t < vTimestampsCam[seq][0])
            ++nextImu;
        // Main loop''')
source = source.replace('            SLAM.TrackMonocular(im,tframe); // TODO change to monocular_inertial', r'''
            vector<ORB_SLAM3::IMU::Point> packet;
            while (nextImu < imu.size() && imu[nextImu].t <= tframe)
                packet.push_back(imu[nextImu++]);
            // Offline integration needs a real measurement after the image boundary.
            // ORB retains this sample in its queue for the next image; no duplicates.
            if (nextImu < imu.size()) packet.push_back(imu[nextImu++]);
            SLAM.TrackMonocular(im,tframe,packet);
''')
source = source.replace("<< keypoints.size() << ',' << associated << '\\n';", r'''
                         << keypoints.size() << ',' << associated << ','
                         << SLAM.GetAtlasForExport()->GetCurrentMap()->GetId() << ','
                         << SLAM.GetAtlasForExport()->GetCurrentMap()->isImuInitialized() << ','
                         << packet.size() << '\n';
            if (ni % 100 == 0) frameSummary.flush();''')
source = source.replace('map_points,ply_file\\n', 'map_points,ply_file,imu_initialized\\n')
source = source.replace("<< valid.size() << \",\" << name << \"\\n\";",
                        '<< valid.size() << "," << name << "," << map->isImuInitialized() << "\\n";')
source = source.replace('    for (ORB_SLAM3::Map* map :', r'''
    ofstream kfExport("KeyFrameMapPoses.csv");
    kfExport << "map_id,synthetic_ns,x,y,z,qx,qy,qz,qw,imu_initialized\n";
    kfExport << setprecision(17);
    for (ORB_SLAM3::Map* map :''')
source = source.replace('        vector<Eigen::Vector3f> valid;', r'''
        for (auto* kf : map->GetAllKeyFrames()) {
            if (!kf || kf->isBad()) continue;
            auto pose = kf->GetPoseInverse();
            auto p = pose.translation(); auto q = pose.unit_quaternion();
            kfExport << map->GetId() << ',' << kf->mTimeStamp*1e9 << ','
                     << p.x() << ',' << p.y() << ',' << p.z() << ','
                     << q.x() << ',' << q.y() << ',' << q.z() << ',' << q.w()
                     << ',' << map->isImuInitialized() << '\n';
        }
        vector<Eigen::Vector3f> valid;''')
source = source.replace('monocular map; coordinates are arbitrary SLAM units',
                        'inertial trial; metric scale only when map IMU initialized')
assert 'IMU_MONOCULAR' in source and 'packet.size()' in source
Path('/opt/ORB_SLAM3/Examples/Monocular/mono_euroc.cc').write_text(source)
