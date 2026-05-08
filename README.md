# ALOV-scripts

Crossplatform-toolkit to evaluate ALOV and assemble release builds.

Most of these tools rely on [`ffmpeg` or `ffprobe`](https://ffmpeg.org) for video decoding and metadata reading and thus require the respective executables.
They are not redistributed with ALOV-scripts to avoid licensing issues and to allow you to update ffmpeg independently.

## ALOV Sanity Checker

The ALOV Sanity Checker is used to largely automate checking ALOV for integrity before release. Thus its releases happen in correspondence with ALOV itself.
The v2 GUI tool for ALOV LE provides an interface to automate the verification of high-fidelity video masters (ProRes MOV) and final game-ready files (Bink) against a strict reference database.

The tool ensures that hundreds of video files across LE1, LE2, LE3 archives adhere to exact project standards before deployment. A CSV table is now used for reference [link here] with data on frame rate, frame count and source location. The sanity checker specifically checks for:
- Frame Mismatches: Detecting if a file's actual frame count differs from the expected vanilla or interpolated count.
- Resolution Drift Verifying that all assets remain at the required 3840x2160 resolution.
- Codec Quality Issues: Ensuring master files use high-quality ProRes profiles (HQ/Standard) and flagging low-bitrate variants like Proxy or LT.
- Missing Interpolated Assets: Identifying assets marked for 60 FPS interpolation that are missing their corresponding master file.
- File Structure Integrity: Validating the presence of assets in correct subdirectories, such as the INTERPOLATED folder hierarchy.

The tool uses two distinct methods for retrieving video information
- FFprobe Integration: For MOV masters, the tool invokes ffprobe to query stream-level metadata, including resolution, framerate, and codec profiles.
- Native Bink Parsing: For .bik files, the tool performs direct binary header reading to extract frame counts and resolution without requiring external codecs or heavy processing.

The engine processes files based on a strict CSV schema, using glob patterns to scan for interpolated master files (e.g., *_60_*.mov) and ensures only one valid master exists per entry.
ProRes profiles are normalized to lowercase strings (e.g., "apple prores 422 hq") to ensure consistency across different FFmpeg versions.
Validation tasks are distributed across a ThreadPoolExecutor, allowing the tool to process multiple assets in parallel. Unlike standard worker threads, this version tracks active subprocess handles; clicking "Cancel" sends a kill signal to all active ffprobe instances to prevent orphaned background processes.

Options
- You can toggle "Ignore Rounding" to allow for +/- 1 frame differences, common in certain container conversions. In ALOV we allow for a 1-frame rounding error when interpolating to 60fps
- Deep Scan Mode: An optional feature that forces ffprobe to read every frame of a video for absolute frame count accuracy, rather than relying on container header metadata.Log Streaming: Real-time logging is displayed in a color-coded UI and simultaneously written to a session-specific text file for audit trails.Smart 
- Archive Filtering: Allow you to target specific sets (e.g., LE1 only) or validate the entire project at once.

## ALOV Batch Binker

The ALOV Batch Binker is used primarily to restore the batch functinality that was stripped out of the publicly available Bink 2 compressor in the Unreal Engine development kit.

This GUI tool is built specifically for the ALOV workflow, with a present CLI argument with limited configration settings. It also features a dynamic ETA system for the overall batch queue. Features include:
- Batch Processing: Qeueue and Bink an entire folder of .mov files into .bik output videos in a single click.
- Live ETA: After each completed file, the tool uses ffprobe to calibrate and updates the ETA countdown based on how long it took to finish the last file, and how many seconds of video are left in the rest of the queued files. Otherwise, the tool falls back to simplified file count-based progress bar.
- Granular Control: ALOV defaults are set, but the data rate, peak data rate, preview frames for bandwidth allocation, and Bink version (inc. legacy Bink 1 for OT ALOV) are included.
- Toggleable Compressor Window: Hidden by default, but the Bink 2 compressor window can be toggled on for live preview of the progress of the current file.
- Persistent Configuration: All config settings and paths are saved to a local JSON file for consistency between sessions.

### Requirements

Both tools make use of ffprobe, though it is optional for the Batch Binker.
Batch Binker requires Bink2ForUnreal. While any version will work with this tool, ALOV for LE specifically requires outputting Bink2 files from version 2022.05
To build locally:

pip install PySide 6

### Legacy

The previous version of ALOV tools by HarHarLinks is located in SanityCheckLegacy.

This repo contains `index`es of the games (`MEX_complete.json`), so I don't expect you'd need to run the `index` mode.
Next to the `MEX_complete.json` databases, there is also `folder_mappings.json`.
This is needed for `--check`, because the directory structure of an ALOV release may not be the same of the installed game.
This mappings file allows the tool to find the correct matching file, even for non-unique file names, and will be updated with the newest ALOV release.
If you are `check`ing something else, e.g. `--intermediate`, and have a different directory structure, you can edit the mappings accordingly.
It maps the actual folder your file is in on the left side to the folder the file will install to on the right side, with the exception of mods which I just store with a certain structure.
