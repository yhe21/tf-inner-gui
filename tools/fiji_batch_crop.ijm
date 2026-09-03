// TF INNER / GLUE batch crop macro for Fiji (ImageJ macro language).
//
// Source images must already have the same portrait orientation used when
// the ROIs were measured (normally 3040 x 4056 after camera rotation).
// The macro never changes the source files. Output uses PNG to avoid a second
// generation of JPEG compression artifacts.

requires("1.53");

    innerInput = getDirectory("Choose the folder containing INNER images");
    glueInput = getDirectory("Choose the folder containing GLUE images");
    outputRoot = getDirectory("Choose an EMPTY output folder");

    // The output folder must not be below either input folder, otherwise a
    // recursive scan could process newly generated files.
    outputLower = toLowerCase(outputRoot);
    if (startsWith(outputLower, toLowerCase(innerInput)) ||
        startsWith(outputLower, toLowerCase(glueInput))) {
        exit("Choose an output folder outside both input folders.");
    }

    Dialog.create("TF Crop Options");
    Dialog.addCheckbox("Pad to square for YOLO Classification", true);
    Dialog.addMessage("INNER output: 320 x 320\nGLUE output: 224 x 224\nPadding value: RGB 114");
    Dialog.show();
    padToSquare = Dialog.getCheckbox();

    innerLeftOutput = outputRoot + "INNER/LEFT/";
    innerRightOutput = outputRoot + "INNER/RIGHT/";
    glueLeftOutput = outputRoot + "GLUE/LEFT/";
    glueRightOutput = outputRoot + "GLUE/RIGHT/";
    makeOutputFolders(outputRoot);

    print("\\Clear");
    print("TF batch crop started");
    setBatchMode(true);

    innerCropCount = processFolder(
        innerInput,
        innerLeftOutput,
        innerRightOutput,
        "INNER",
        padToSquare
    );
    glueCropCount = processFolder(
        glueInput,
        glueLeftOutput,
        glueRightOutput,
        "GLUE",
        padToSquare
    );

    setBatchMode(false);
    showMessage(
        "TF Batch Crop Complete",
        "INNER crops saved: " + innerCropCount +
        "\nGLUE crops saved: " + glueCropCount +
        "\n\nOutput folder:\n" + outputRoot
    );

function makeOutputFolders(outputRoot) {
    File.makeDirectory(outputRoot + "INNER");
    File.makeDirectory(outputRoot + "INNER/LEFT");
    File.makeDirectory(outputRoot + "INNER/RIGHT");
    File.makeDirectory(outputRoot + "GLUE");
    File.makeDirectory(outputRoot + "GLUE/LEFT");
    File.makeDirectory(outputRoot + "GLUE/RIGHT");
}

function processFolder(inputFolder, leftOutput, rightOutput, kind, padToSquare) {
    cropCount = 0;
    fileList = getFileList(inputFolder);

    for (i = 0; i < fileList.length; i++) {
        name = fileList[i];
        fullPath = inputFolder + name;

        if (File.isDirectory(fullPath)) {
            cropCount = cropCount + processFolder(
                fullPath,
                leftOutput,
                rightOutput,
                kind,
                padToSquare
            );
        } else if (isSupportedImage(name)) {
            cropCount = cropCount + processImage(
                fullPath,
                name,
                leftOutput,
                rightOutput,
                kind,
                padToSquare
            );
        }
    }
    return cropCount;
}

function processImage(fullPath, fileName, leftOutput, rightOutput, kind, padToSquare) {
    open(fullPath);
    sourceImageId = getImageID();
    getDimensions(imageWidth, imageHeight, channels, slices, frames);

    // The largest configured ROI ends at x=2412 and y=2760. The additional
    // portrait check catches unrotated 4056 x 3040 camera images.
    if (imageWidth < 2412 || imageHeight < 2760 || imageWidth >= imageHeight) {
        print("[SKIP: size/orientation] " + fullPath +
            " (" + imageWidth + " x " + imageHeight + ")");
        close();
        return 0;
    }

    baseName = File.getNameWithoutExtension(fileName);
    saved = 0;

    if (kind == "INNER") {
        // Left:  x=1428, y=2444, width=216, height=316
        // Right: x=2196, y=2436, width=216, height=308
        saved = saved + saveCrop(
            sourceImageId, 1428, 2444, 216, 316,
            320, padToSquare,
            leftOutput + baseName + "_LEFT.png"
        );
        saved = saved + saveCrop(
            sourceImageId, 2196, 2436, 216, 308,
            320, padToSquare,
            rightOutput + baseName + "_RIGHT.png"
        );
    } else {
        // Left:  x=1436, y=2456, width=208, height=88
        // Right: x=2188, y=2448, width=212, height=96
        saved = saved + saveCrop(
            sourceImageId, 1436, 2456, 208, 88,
            224, padToSquare,
            leftOutput + baseName + "_LEFT.png"
        );
        saved = saved + saveCrop(
            sourceImageId, 2188, 2448, 212, 96,
            224, padToSquare,
            rightOutput + baseName + "_RIGHT.png"
        );
    }

    selectImage(sourceImageId);
    close();
    showProgress(1, 1);
    return saved;
}

function saveCrop(sourceImageId, x, y, width, height, squareSize, padToSquare, outputPath) {
    if (File.exists(outputPath)) {
        print("[SKIP: output exists] " + outputPath);
        return 0;
    }

    selectImage(sourceImageId);
    makeRectangle(x, y, width, height);
    run("Duplicate...", "title=TF_CROP_TEMP");

    if (padToSquare) {
        setBackgroundColor(114, 114, 114);
        run(
            "Canvas Size...",
            "width=" + squareSize +
            " height=" + squareSize +
            " position=Center"
        );
    }

    saveAs("PNG", outputPath);
    close();
    return 1;
}

function isSupportedImage(fileName) {
    lowerName = toLowerCase(fileName);
    return endsWith(lowerName, ".jpg") ||
        endsWith(lowerName, ".jpeg") ||
        endsWith(lowerName, ".png") ||
        endsWith(lowerName, ".tif") ||
        endsWith(lowerName, ".tiff");
}
