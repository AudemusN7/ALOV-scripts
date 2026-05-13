# ALOV Sanity Checker CSV Table Overview

The CSV table used for the sanity checker requires a specific format to work, as the ALOV tooling and workflow is highly specific.
1. File Name
* The source name of the Bink video in the game. The **.BIK** extension is used but while in ProRes mode it looks for the **.MOV** extension for the same file name as well.
* Additionally, any interpolated MOV video must have the ``_60_*`` suffix (eg. GLO_01_Relay_60_APOLLO.mov)
* Any video that does not match the file name exactly will be flagged as a **GHOST FILE**
2. Source
* For organisational purposes, a specific file structure is required. From the input root directory, the tool looks for subfolders to determine if a file is in the correct location
* Example - If the main directory is called **ALOVArchive** then it expects subfolders of **LE1, LE2** and **LE3**. With **BASEGAME/DLC** subfolders inside. And inside those subfolders an **INTERPOLATED** folder for 60fps versions
3. Vanilla Frames
* The original frame count of the source video
4. Interpolated Frames
* This can be whatever you want, but for ALOV it's always exactly **2x** the Vanilla FPS
5. Is Interpolated
* This column has 3 valid inputs. **YES, NO** and **YES (BIK ONLY)**. 
* If flagged as **YES**, the tool will look for a MOV with a vanilla frame rate AND a 2x framerate version in the **INTERPOLATED** subfolder
* If flagged as **NO**, the tool will skip looking for the associated **INTERPOLATED** version
* If flagged as **YES (BIK Only)** the tool will skip looking for the interpolated version in **MOV mode**, but will check for a 2x frame count in **BIK mode** (this is to handle some cases such as in-game remakes where the base MOV is already 60fps)
6. Excluded (MOV)
* This column can be marked **YES** or **NO**
* If **YES**, the tool will skip looking for the file entirely while in **MOV** mode
* If **NO**, the tool will check for the **MOV** file as normal
7. Excluded (BIK)
* This column can be marked **YES** or **NO**
* If **YES**, the tool will skip looking for the file entirely while in **BIK** mode
* If **NO**, the tool will check for the **BIK** file as normal
8. Exclusion Reason
* This column is not used by the sanity checker, and is just used for comments
