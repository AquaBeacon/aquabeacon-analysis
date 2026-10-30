"""Patch the existing EuRoC adapter to preserve diagnostics when no map initialized."""
from pathlib import Path

path = Path('/opt/ORB_SLAM3/Examples/Monocular/mono_euroc.cc')
source = path.read_text()
source = source.replace('#include<chrono>', '#include<chrono>\n#include<cstdlib>')
source = source.replace('    ofstream features("TrackedFeatures.csv");',
                        '    cv::setNumThreads(2);\n'
                        '    ofstream frameSummary("TrackedFrameSummary.csv");\n'
                        '    frameSummary << "frame_index,utc_ns,tracking_state,detected_features,'
                        'associated_features\\n";\n'
                        '    ofstream features("TrackedFeatures.csv");')
source = source.replace('            for (size_t k = 0; k < keypoints.size(); ++k)',
                        '            size_t associated = 0;\n'
                        '            for (size_t k = 0; k < keypoints.size(); ++k)')
source = source.replace("                features << ni << ',' << stamp << ',' << state",
                        '                const bool valid = k < points.size() && points[k] '
                        '&& !points[k]->isBad();\n'
                        '                if (valid) ++associated;\n'
                        '                if (!valid && k % 10 != 0) continue;\n'
                        "                features << ni << ',' << stamp << ',' << state")
source = source.replace("                features << '\\n';\n            }",
                        "                features << '\\n';\n            }\n"
                        "            frameSummary << ni << ',' << stamp << ',' << state << ','\n"
                        "                         << keypoints.size() << ',' << associated << '\\n';")
source = source.replace(' + ".png"',
                        ' + (getenv("ORB_IMAGE_EXTENSION") ? getenv("ORB_IMAGE_EXTENSION") '
                        ': ".png")')
source = source.replace('    // Stop all threads\n',
                        '    features.flush();\n    frameSummary.flush();\n'
                        '    // Stop all threads\n')
source = source.replace('    // Save camera trajectory\n',
                        '    bool hasKeyframes = false;\n'
                        '    for (auto* map : SLAM.GetAtlasForExport()->GetAllMaps())\n'
                        '        if (map && map->KeyFramesInMap() > 0) hasKeyframes = true;\n'
                        '    if (hasKeyframes) {\n'
                        '    // Save camera trajectory\n')
source = source.replace('    // Export each surviving Atlas map separately:',
                        '    } else {\n'
                        '        ofstream("CameraTrajectory.txt");\n'
                        '        ofstream("KeyFrameTrajectory.txt");\n'
                        '        cout << "No initialized map: empty trajectories saved" << endl;\n'
                        '    }\n'
                        '    // Export each surviving Atlas map separately:')
if 'bool hasKeyframes' not in source or 'features.flush()' not in source:
    raise ValueError('Expected patched monocular source is unavailable')
path.write_text(source)
