OpenScope GUI {version} for Windows 11 (64-bit)
===============================================

A desktop window for the FNIRSI 2C53T oscilloscope / multimeter when it runs the
OpenScope firmware: live traces, the meter, screenshots, a calibration backup and
firmware updates over USB. You do not need Python.

  Program commit    {gui_commit}
  Bundled firmware  firmware\{image_name}
                    OpenScope guest-coldtrace {firmware_rev}, built {build_time},
                    API level {api_level}


1. Start
--------

  1. Unzip the whole zip file to a folder, for example your Documents folder.
     Do not start the program from inside the zip file.
  2. Double-click openscope-gui.exe.

  The program is not signed. The first time, Windows SmartScreen can show
  "Windows protected your PC". Click "More info", then "Run anyway". Do this
  only if you got the zip file from the OpenScope-2C53T-GUI release page on
  GitHub and its SHA-256 is the same as in SHA256SUMS.txt
  (PowerShell: Get-FileHash <zip file>).


2. Connect the unit
-------------------

  Connect the unit to the computer with a USB data cable and switch it on. The
  program finds the unit by itself. Windows uses its own USB serial driver
  (usbser.inf, "USB Serial Device (COMn)"): you do not install a driver.

  If the unit still runs the FNIRSI firmware, the program cannot connect. A
  yellow line tells you this when it sees the unit, and its button starts the
  first install (see 4).


3. Back up the calibration first
--------------------------------

  Each unit has its own calibration, stored in the unit. If it is lost, it
  cannot be made again. Before you change the firmware:

  1. Device > Back up calibration...
  2. When the backup is done, click "Open folder".
  3. Copy that folder to a second place that is not this computer (a USB stick
     or a cloud drive).

  The backups are in %LOCALAPPDATA%\OpenScope\backups\<unit ID>\<date and time>.
  The firmware update makes one more backup by itself and keeps the update log
  in the same folder.


4. Update the firmware
----------------------

  Device > Update firmware... (or the "Update firmware..." button on the yellow
  line). The wizard uses the image in the firmware folder of this package and:

  1. checks the unit and the image;
  2. makes a backup and checks it (it does not go on without a good backup);
  3. tells you how to put the unit in upgrade mode: keep USB connected, hold
     MENU and tap Power, keep MENU held until the upgrade screen shows. If it
     does not show: hold MENU, press the reset button in the small hole with a
     pin, keep MENU held 3 to 5 seconds;
  4. copies the image (about 11 seconds). Do not unplug the unit and do not
     open its drive while it copies;
  5. waits for the unit to start again (up to 60 seconds);
  6. shows the result: the firmware against the image, and the calibration
     before and after.

  First install from the FNIRSI firmware: the same wizard. You must tick the
  acknowledgment and type INSTALL. The backup comes after the first OpenScope
  start. Do not press a key on the unit until the wizard says that the backup
  is done.

  Never copy an image to the unit's drive with Explorer: a second write during
  the copy damages the image.


5. If the update stops (recovery)
---------------------------------

  The unit keeps its factory loader. If the unit does not start after an
  update, or the screen stays on the upgrade screen:

  1. Keep USB connected. Hold MENU, press the reset button in the small hole,
     keep MENU held 3 to 5 seconds. A drive with the name IAP opens.
  2. Start openscope-gui.exe. The yellow line says that the unit waits in
     upgrade mode. Click "Update firmware..." and do the wizard again.
     (Python users can also run "openscope update" from the firmware
     repository: tools/openscope_host.)


6. Back to the FNIRSI firmware
------------------------------

  This package does NOT hold the FNIRSI stock firmware. To go back to it you
  need your own copy of the FNIRSI file (for example APP_2C53T_V1.2.0_251015.bin
  from FNIRSI). The return to stock is a command of the openscope library only
  (Python): openscope update --stock-image <your FNIRSI file>. See the README of
  the firmware repository, tools/openscope_host.


7. Release notes: what you must know
------------------------------------

  * Volts are not calibrated across units. The scale in volts comes from the
    measurements of one unit. On another unit some ranges were wrong by up to
    67 %. Use the volts only to compare signals on your own unit. The plot
    shows raw ADC counts.
  * The meter reads DC volts only in this firmware.
  * Not all time/div settings are measured. The window hides the settings with
    no measured value (View > Show uncalibrated settings shows them).
  * At the slowest time/div settings the frequency from one record can be
    about 3 % too high (the records have a seam).
  * A setting changed just after a change from the slowest time/div can be
    refused one time ("not ready"). Do it again.
  * In STOP, or after a Single capture, the unit's screen does not draw again,
    but its keys still change settings. Press RUN (or Run in the window) and
    set the trigger to Auto to see the screen again.
  * An open settings menu on the unit does not show a change made from the
    computer until you open the menu again.
  * The Math function on the unit's screen does not work (it shows for one
    or two frames).
  * The Multimeter and Signal generator tabs have no control panel yet.


8. Licences and source
----------------------

  OpenScope GUI and the openscope library: GNU GPL version 3 or later
  (LICENSE.txt). Other parts: THIRD-PARTY-NOTICES.txt. Where to get the source,
  and the written offer: firmware\SOURCE.txt.

  The firmware image holds the FPGA configuration data of the instrument, which
  was taken from the FNIRSI stock firmware. It is not GPL source code. See
  firmware\SOURCE.txt, part 4.

  This program and firmware come with NO WARRANTY. You change the firmware of
  your unit at your own risk.
