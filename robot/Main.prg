#define X_offset -0.2
#define Y_offset -0.1
Real Distance
Global Preserve Integer Count, NP_Loc, Layer
Global Preserve Boolean isLastFilled, isBufferLoaded
Integer Mcount, conveyorCount
Boolean MainTool, firstLoc
Global Preserve Real temp_cnt_hmi, tot_cnt_hmi
Real NPX, NPY, NPZ, NPU, NPSX, NPSY, NPSZ, NPSU, DROPX, DROPY, DROPZ, DROPU
Global Boolean RpiConnected, RpiFatalPending, RpiFatalSent
Global String RpiFatalMessage$
Function main
	'Call pallet_build
	'Quit All
	Motor On
	Power_Low

	'Speed 15
	'Accel 50, 50
	'SpeedS 300
	'SpeedR 500
	'AccelR 500
	Call Init
	
	Call Power_High
	Off finishedPickPart
	OutReal 36, tot_cnt_hmi
	
	Do
		Print Tmr(1)
		TmReset (1)
		VT6_Cycle
	Loop
Fend
Function RpiNet
	Integer netState, tokenCount, i
	Real calibValues(11)
	String response$
	String fields$(0)
	
	' This task is the only owner of TCP port #201.
	OnErr GoTo NetError
	
NetReconnect:
	RpiConnected = False
	CloseNet #201
	Wait 0.2
	OpenNet #201 As Client
	WaitNet #201, 2
	netState = ChkNet(201)
	If netState < 0 Then
		CloseNet #201
		Wait 1
		GoTo NetReconnect
	EndIf
	
	Print "RPi TCP connected"
	RpiConnected = True
	
	Do
		netState = ChkNet(201)
		If netState < 0 Then
			GoTo NetDisconnected
		EndIf
		
		' One shared fatal-message channel replaces separate error I/O bits.
		If RpiFatalPending = True Then
			Print #201, RpiFatalMessage$
			RpiFatalPending = False
			RpiFatalSent = True
		EndIf
		
		' CALIB has priority because Pick_NP uses these values.
		If MemSw(RpiCalibReq) = On Then
			Print #201, "CALIB"
			MemOff RpiCalibReq
		EndIf
		
		If MemSw(RpiInnerReq) = On Then
			Print #201, "INNER"
			MemOff RpiInnerReq
		EndIf
		
		If MemSw(RpiGlueReq) = On Then
			Print #201, "GLUE"
			MemOff RpiGlueReq
		EndIf
		
		' Read only complete CR/LF terminated lines, so this loop never
		' blocks robot motion while YOLO is still processing an image.
		If Lof(201) > 0 Then
			Line Input #201, response$
			response$ = UCase$(Trim$(response$))
			
			Select response$
			Case "INNER,OK"
				Print "INNER OK"
			Case "INNER,NG"
				Print #201, "NO_INNER"
				Wait 0.05
				Quit All
			Case "GLUE,OK"
				Print "GLUE OK"
			Case "GLUE,NG"
				Print #201, "NO_GLUE"
				Wait 0.05
				Quit All
			Default
				' Apply every CALIB value that arrived. Missing values keep
				' their current value, which is zero after initialization.
				tokenCount = ParseStr(response$, fields$(), ",")
				If tokenCount > 12 Then
					tokenCount = 12
				EndIf
				If tokenCount > 0 Then
					For i = 0 To tokenCount - 1
						calibValues(i) = Val(fields$(i))
						If Abs(calibValues(i)) <= 0.5 Then
							Select i
							Case 0
								NPX = calibValues(i)
							Case 1
								NPY = calibValues(i)
							Case 2
								NPZ = calibValues(i)
							Case 3
								NPU = calibValues(i)
							Case 4
								NPSX = calibValues(i)
							Case 5
								NPSY = calibValues(i)
							Case 6
								NPSZ = calibValues(i)
							Case 7
								NPSU = calibValues(i)
							Case 8
								DROPX = calibValues(i)
							Case 9
								DROPY = calibValues(i)
							Case 10
								DROPZ = calibValues(i)
							Case 11
								DROPU = calibValues(i)
							Send
						Else
							Print "Ignored CALIB value outside +/-0.5: ", fields$(i)
						EndIf
					Next i
				Else
					Print "Empty RPi response ignored"
				EndIf
			Send
		EndIf
		
		Wait 0.01
	Loop
	
NetDisconnected:
	RpiConnected = False
	Print "RPi TCP disconnected; reconnecting"
	CloseNet #201
	Wait 1
	GoTo NetReconnect
	
NetError:
	RpiConnected = False
	Print "RPi TCP error ", Err, ": ", ErrMsg$(Err)
	EResume NetReconnect
Fend
Function FatalError(errorCode$ As String)
	' Best-effort logging only: RPi must never prevent the robot stopping.
	Print errorCode$
	RpiFatalMessage$ = UCase$(errorCode$)
	RpiFatalSent = False
	If RpiConnected = True Then
		RpiFatalPending = True
		Wait RpiFatalSent = True, 0.2
		If TW = True Then
			Print "Unable to send error to Raspberry Pi: ", errorCode$
		EndIf
	EndIf
	Quit All
Fend
Function Power_High
	Power High
	Speed 95
	Accel 120, 120
	SpeedR 1000
	AccelR 1000
	SpeedS 1900
	AccelS 5000, 5000
Fend
Function Power_Mid
	Power High
	Speed 95
	Accel 110, 110
	SpeedR 1000
	AccelR 1000
	SpeedS 1500
	AccelS 3500, 3500
Fend
Function Power_Low
	Power Low
	Speed 15
	Accel 15, 15
	SpeedS 500
	AccelS 1000, 1000
Fend
Function Init
	' Only the three command request bits are exposed as Memory I/O.
	MemOff RpiInnerReq
	MemOff RpiGlueReq
	MemOff RpiCalibReq
	RpiConnected = False
	RpiFatalPending = False
	RpiFatalSent = False
	RpiFatalMessage$ = ""
	
	' TCP is optional. RpiNet reconnects in the background and main continues.
	Xqt RpiNet
	
	Off finishedDropNP
	Off finishedPickPart
	Off finishedPutPart
	Off plateEjectReq
	Off secondPlaceReq
	Off vacuumSol1
	Off vacuumSol2
	Off motroRun
	TmReset (4)
	firstLoc = False
	NPX = 0; NPY = 0; NPZ = 0; NPU = 0;
	NPSX = 0; NPSY = 0; NPSZ = 0; NPSU = 0;
	DROPX = 0; DROPY = 0; DROPZ = 0; DROPU = 0
	Tool 1
	If (CX(Here) > 400) Then
		Pass P_Pick_NP1
		
		Go Here :X(430) LJM
		Go T_PP_DPH_2 LJM
		Go T_PP_DPH_1 LJM
		Go P_Pick_Part1 LJM
	ElseIf (CY(Here) > 400) Then
		Go Here +Z(50) LJM
		Go Here :Y(400) LJM
        Move Here :Z(550)
		Go P_Pick_Part1 LJM
	Else
		Pass Here :Z(550) LJM
		Go P_Pick_Part1 LJM
	EndIf
		UpdateLayer
	On finishedDropNP; Wait 0.15; On finishedPickPart; On finishedPutPart
	Wait Sw(dropNPReq) = On And Sw(pickPartReq) = On And Sw(putPartReq) = On
	Wait 0.5
	Off finishedDropNP; Off finishedPickPart; Off finishedPutPart
	Wait Sw(dropNPReq) = Off And Sw(pickPartReq) = On And Sw(putPartReq) = On
Fend
Function main1
	Init_Reset
Fend
Function Init_Reset
	Motor On
	Power_Low
	Count = 1
	NP_Loc = 1
	isLastFilled = False
	isBufferLoaded = False
	UpdateLayer
	Off finishedDropNP
	Off finishedPickPart
	Off finishedPutPart
	Off plateEjectReq
	Off vacuumSol1
	Off vacuumSol2
	Wait 0.5
	If (Sw(okPosition) = On) Then
		On plateEjectReq
		Wait 0.5
		Wait Sw(okPosition) = Off
		Wait Sw(ejecting) = On
		Off plateEjectReq
		Off secondPlaceReq
	EndIf
	Wait 1
	UpdateLayer
	Wait Sw(okPosition) = On
	
	'Init
Fend
Function VT6_Cycle
	
	Call Pick_Part
	Call Drop_Part
	Call Pick_NP
	Call Drop_NP
	Call Pick_Fixture
	Call Drop_Pallet
Fend
Function Pick_Part
	' Request offsets without waiting; current/default values remain usable.
	MemOn RpiCalibReq
	
	'Move P_pick_Part1 CP
	Move P_Pick_Part +Z(125) +X(33.5) CP
	If MemSw(IsPickOk) = False Then
		If firstLoc = True Then
			Wait MemSw(IsPickOk) = True, 150
			If TW = True Then
				FatalError "NO_PART_FROM_CONVEYOR"
			EndIf
		Else
			Wait MemSw(IsPickOk) = True
			If Tmr(4) > 300 Then
				firstLoc = True
			EndIf
		EndIf
	EndIf
	
	Move P_Pick_Part
	On vacuumSol1
	On vacuumSol2
	Wait 0.25

	Move P_Pick_Part +Z(125) +X(33.5) CP
	'Go Here +Z(50) LJM CP
	If (Sw(vacuumSensor1) = Off Or Sw(vacuumSensor2) = Off) Then
		'to balance parts on track
		If (Sw(vacuumSensor1) = Off And Sw(vacuumSensor2) = Off) Then
			Move P_Pick_Part +Z(125) +X(133.5) +Y(150); Off vacuumSol1;	Off vacuumSol2
		ElseIf Sw(vacuumSensor1) = Off Then
			Move P_Pick_Part +Z(125) +X(133.5) +Y(150); Off vacuumSol1;	Off vacuumSol2
			Wait 1; Wait MemSw(IsPickOk) = True
			Move P_Pick_Part +Z(125) +X(33.5) CP; Move P_Pick_Part
			On vacuumSol1; Wait 0.5
			Move P_Pick_Part +Z(125) +X(33.5) CP
			Move P_Pick_Part +Z(125) +X(133.5) +Y(150); Off vacuumSol1;	Off vacuumSol2
			If Sw(vacuumSensor1) = Off Then
				FatalError "PICK_PART_NO_VAC"
			EndIf
		ElseIf Sw(vacuumSensor2) = Off Then
			Move P_Pick_Part +Z(125) +X(133.5) +Y(150); Off vacuumSol1;	Off vacuumSol2
			Wait 1; Wait MemSw(IsPickOk) = True
			Move P_Pick_Part +Z(125) +X(33.5) CP; Move P_Pick_Part
			On vacuumSol2; Wait 0.5
			Move P_Pick_Part +Z(125) +X(33.5) CP
			Move P_Pick_Part +Z(125) +X(133.5) +Y(150); Off vacuumSol1;	Off vacuumSol2
			If Sw(vacuumSensor2) = Off Then
				FatalError "PICK_PART_NO_VAC"
			EndIf
		EndIf
		
		Move P_Pick_Part +Z(125) +X(133.5) +Y(150); Off vacuumSol1;	Off vacuumSol2
		Wait 1
		Wait MemSw(IsPickOk) = True
		Move P_Pick_Part +Z(125) +X(33.5) CP
		Move P_Pick_Part
		On vacuumSol1
		On vacuumSol2
		Wait 0.5
		Move P_Pick_Part +Z(125) +X(33.5) CP
		If (Sw(vacuumSensor1) = Off Or Sw(vacuumSensor2) = Off) Then
			FatalError "PICK_PART_NO_VAC"
		EndIf
	EndIf

	Move P_pick_Part1 CP
	Move T_PP_DPH_1 CP
	'pass T_PP_DPH_1 ljm
	If (False Or (Sw(putPartReq) = On And Sw(dropNPReq) = Off)) Then
	
	Else
		Wait Sw(putPartReq) = On And Sw(dropNPReq) = Off, 25
		If TW = True Then
			FatalError "PICK_PART_WAIT_TIMEOUT"
		EndIf
	EndIf
	'wait for T6 signal
		Move T_PP_DPH_2 CP
		'Pass T_PP_DPH_2 LJM

	
Fend
Function Drop_Part
	'todo remove true
	If (False Or (Sw(putPartReq) = On And Sw(pickPartReq) = Off And Sw(dropNPReq) = Off)) Then
	Else
		Wait Sw(putPartReq) = On And Sw(pickPartReq) = Off And Sw(dropNPReq) = Off, 25
		If TW = True Then
			FatalError "DROP_PART_WAIT_TIMEOUT"
		EndIf
	EndIf
	
	' The fixture is ready. Trigger INNER without waiting for inference.
	MemOn RpiInnerReq
	
	On motroRun
	Xqt Search_pallet
	Power_Mid
	Move P_Drop_Part +Z(50) CP
	Move P_Drop_Part +Z(17) +W(-10) +X(17) CP; Move P_Drop_Part +Z(17) +W(-10) +X(8); Move P_Drop_Part +Z(5) CP ! D70; Off vacuumSol1; Off vacuumSol2 !
	'Pass P_Drop_Part +Z(50) LJM
	Move P_Drop_Part +Z(3)

	Wait 0.15
	
	
	Move P_Drop_Part +Z(25) CP ! D50; On finishedPutPart; Off motroRun !
	
	
	Pass P_Pick_NP +Z(45) LJM
	 ' todo handle off
Fend
Function Pick_NP
	If (NP_Loc = 1) Then
		Go P_Pick_NP +X(NPX) +Y(NPY) +Z(6 + NPZ) +U(NPU) LJM
		Go P_Pick_NP +X(NPX) +Y(NPY) +Z(NPZ) +U(NPU) LJM ! D30; On vacuumSol1; On vacuumSol2 !
		NP_Loc = 2
	ElseIf (NP_Loc = 2) Then
		Go P_Pick_NPS +X(NPSX) +Y(NPSY) +Z(6 + NPSZ) +U(NPSU) LJM
		Go P_Pick_NPS +X(NPSX) +Y(NPSY) +Z(NPSZ) +U(NPSU) LJM ! D30; On vacuumSol1; On vacuumSol2 !
        NP_Loc = 1
	Else
		FatalError "NP_LOCATION_INVALID"
	EndIf
	Wait 0.4
	If (Sw(vacuumSensor1) = Off Or Sw(vacuumSensor2) = Off) Then
		Wait 0.4
	EndIf
	Pass Here +Z(10) LJM
	Pass Here +Z(25) LJM
	
	
	Go P_Drop_NP1 +W(-5) +X(2.8) LJM
	If (Sw(vacuumSensor1) = Off Or Sw(vacuumSensor2) = Off) Then
		Wait 0.7
	EndIf
    If (Sw(vacuumSensor1) = Off Or Sw(vacuumSensor2) = Off) Then
    	If Sw(vacuumSensor1) = Off Then
    		Off vacuumSol1
    	EndIf
    	If Sw(vacuumSensor2) = Off Then
    		Off vacuumSol2
    	EndIf
    	Go P_Pick_NPS +Z(35) LJM
		FatalError "PICK_NP_NO_VAC"
	EndIf
	'Go P_Drop_NP1 LJM
	Wait Sw(putPartReq) = Off
	Off finishedPutPart
Fend
Function Drop_NP
	If (False Or (Sw(putPartReq) = Off And Sw(pickPartReq) = Off And Sw(dropNPReq) = On)) Then
	Else
		Wait Sw(putPartReq) = Off And Sw(pickPartReq) = Off And Sw(dropNPReq) = On, 25
		If TW = True Then
			FatalError "DROP_NP_WAIT_TIMEOUT"
		EndIf
	EndIf
	
	' Glue is visible now. Trigger GLUE before placing the NP.
	MemOn RpiGlueReq
	
	Power_High
	Move P_Drop_NP +X(DROPX) +Y(DROPY) +Z(10 + DROPZ) +U(DROPU) +W(-5) +X(3) CP
	Move P_Drop_NP +X(DROPX) +Y(DROPY) +Z(3.5 + DROPZ) +U(DROPU) +W(-5) +X(2.2); '
	Move P_Drop_NP +X(DROPX) +Y(DROPY) +Z(2.5 + DROPZ) +U(DROPU) +W(-5) +X(4.2); '
	Move P_Drop_NP +X(2.2 + DROPX) +Y(DROPY) +Z(1.3 + DROPZ) +U(DROPU) ROT ! D20; Off vacuumSol1; Off vacuumSol2; !
	Move P_Drop_NP +X(3 + DROPX) +Y(DROPY) +Z(2.5 + DROPZ) +U(DROPU) CP
	'Move P_Drop_NP +Z(3) CP
	Move P_Drop_NP +W(-1) +Z(25) ! D50; On finishedDropNP !
	
Fend
Function Pick_Fixture
	If (False Or (Sw(putPartReq) = Off And Sw(pickPartReq) = On And Sw(dropNPReq) = Off)) Then
	Else
		Wait Sw(putPartReq) = Off And Sw(pickPartReq) = On And Sw(dropNPReq) = Off, 25
		If TW = True Then
			FatalError "PICK_FIXTURE_WAIT_TIMEOUT"
		EndIf
	EndIf
	Off finishedDropNP
	Move P_Drop_NP +X(DROPX) +Y(DROPY) +Z(3 + DROPZ) +U(DROPU)
	On vacuumSol1
	On vacuumSol2
	Wait 0.25
	Power_High
	Move P_Drop_NP +Z(45) CP
	If (Sw(vacuumSensor1) = Off Or Sw(vacuumSensor2) = Off) Then
		FatalError "PICK_FIXTURE_NO_VAC"
	EndIf
Fend
Function Drop_Pallet
	Tool 1
	Move T_PF_E_1 CP
	Move T_PF_E_2 CP
	Move P_Mid CP ! D15; On finishedPickPart !
	'Pass T_PF_E_1 LJM
	'Pass T_PF_E_2 LJM
	
	'Pass P_Mid LJM

	If (MemSw(IsPalletOk) = On) Then
		
	Else
		Wait MemSw(IsPalletOk) = On, 15
	EndIf
	If (Sw(vacuumSensor1) = Off Or Sw(vacuumSensor2) = Off) Then
		FatalError "PICK_FIXTURE_NO_VAC"
	EndIf
	'error handler overtime
	If (MemSw(IsPalletOk) = Off) Then
		FatalError "PALLET_NOT_READY"
	EndIf
	'pallet start:
	If (Count <= 2) Then
		If MemSw(IsPickOk) = On Then 'advance conveyor to prevent jamming
			TmReset (3)
		EndIf
		Move P_Pallet +Z(50) CP; Move P_Pallet
		Off vacuumSol1
		Wait 0.2
		Tool 2
		
		Move Here +Z(35) CP; Move P_Pallet2 +Z(50) CP; Move P_Pallet2
		Off vacuumSol2
		Wait 0.2
		Tool 1
		Count = Count + 1
		Move Here +Z(35) CP

		
	ElseIf (Count <= 10) Then

		Move P_Pallet +Z(50) CP; Move P_Pallet
		'Pass P_Pallet +Z(25) LJM; Go P_Pallet LJM
		Off vacuumSol1
		Off vacuumSol2
		Wait 0.2
		Count = Count + 1
		Move P_Pallet +Z(35) CP
	ElseIf (Count <= 18) Then
		
		Move P_Pallet +Z(50) CP; Move P_Pallet
		'Pass P_Pallet +Z(25) LJM; Go P_Pallet LJM
		Off vacuumSol1
		Off vacuumSol2
		Wait 0.2
		Count = Count + 1
		
		Move P_Pallet +Z(35) CP
		If (Count > 18) Then
			On secondPlaceReq
		EndIf
	ElseIf (Count <= 22) Then
		
		Move P_Pallet +Z(50) CP; Move P_Pallet
		'Pass P_Pallet +Z(25) LJM; Go P_Pallet LJM
		Off vacuumSol1
		Off vacuumSol2
		Wait 0.2
		Count = Count + 1
		Move P_Pallet +Z(35) CP
		If (Count = 23) Then
			
			If (isBufferLoaded = True) Then
				If MemSw(IsPickOk) = On Then 'advance conveyor to prevent jamming
					TmReset (3)
				EndIf
				Go Here :Z(500) LJM CP
				Move Here :Y(570) CP
				Move P_Buffer +Z(30) CP
				Move P_Buffer
				On vacuumSol1
				Wait 0.3
				P_Pallet = Last +Z(-60 * (Layer - 1)) +X(X_offset * (Layer - 1)) +Y(Y_offset * (Layer - 1))
				Move P_Buffer +Z(50) CP
				Move P_Pallet :Z(500)
				If Sw(vacuumSensor1) = Off Then
					FatalError "PICK_FIXTURE_NO_VAC"
				EndIf
				Go P_Pallet +Z(50) LJM
				Move P_Pallet
				Off vacuumSol1
				Wait 0.3
				Move P_Pallet +Z(50)
				Go Here :Z(500)
				
				On plateEjectReq
				Wait Sw(ejecting) = On
				Off plateEjectReq
				Off secondPlaceReq
				Count = 1
				isBufferLoaded = False
			Else
				Count = 23
			EndIf
		EndIf
	ElseIf (Count = 23) Then
		If MemSw(IsPickOk) = On Then 'advance conveyor to prevent jamming
			TmReset (3)
		EndIf
		Tool 2
		Move P_Buffer +Z(30)
		Move P_Buffer
		Off vacuumSol2
		Wait 0.3
		isBufferLoaded = True
		Tool 1
		P_Pallet = Last +Z(-60 * (Layer - 1)) +X(X_offset * (Layer - 1)) +Y(Y_offset * (Layer - 1))
		Move Here +Z(50) CP
		Move P_Pallet :Z(500) CP
		Go P_Pallet +Z(50) LJM CP
		Move P_Pallet
		Off vacuumSol1
		Wait 0.3
		Move P_Pallet +Z(50) CP
		Go Here :Z(500) LJM CP
		On plateEjectReq
		Wait Sw(ejecting) = On
		Off plateEjectReq
		Off secondPlaceReq
		Count = 1
	Else
		Print "Count error"
	EndIf
	

	Move T_DF_PP CP
	'Pass T_DF_PP
	Off finishedPickPart
	MemOff IsPalletOk
	Xqt updateHmiNumbers
	
Fend
Function Search_pallet

	'Pass T_PF_E_1 LJM
	'Pass T_PF_E_2 LJM
	
	'Pass P_Mid LJM
	MemOff IsPalletOk
	Do While IsLayerChanged
		UpdateLayer
		Count = 1
	Loop
	If (Sw(ejecting) = On) Then
		Off secondPlaceReq
		On plateEjectReq
		Wait Sw(ejecting) = Off
		Off plateEjectReq
		Count = 1
		isLastFilled = False
		Wait 0.25
		UpdateLayer
	EndIf
	If Sw(okPosition) = Off Then
		Wait Sw(okPosition) = On, 15
	EndIf
	'error handler overtime
	If (Sw(okPosition) = Off) Then
		Print "OKPosition not at ok after 15 second"
	EndIf

	'pallet start:
	If (Count <= 2) Then
		Off secondPlaceReq
		Wait 0.1
		
		Wait Sw(secondPlaceOn) = Off, 15
		Wait Sw(okPosition), 10
		If (Sw(okPosition) = Off) Then
			FatalError "SECOND_PLACE_TIMEOUT"
		EndIf
		P_Pallet = Pallet(3, (Count) * 2 - 1) +Z(-60 * (Layer - 1)) +X(X_offset * (Layer - 1)) +Y(Y_offset * (Layer - 1))
		P_Pallet2 = Pallet(3, (Count) * 2) +Z(-60 * (Layer - 1)) +X(X_offset * (Layer - 1)) +Y(Y_offset * (Layer - 1))
		
	ElseIf (Count <= 10) Then

		If Sw(secondPlaceOn) = On Then
			Off secondPlaceReq
			Wait 0.5
			Wait Sw(secondPlaceOn) = Off, 15
		EndIf
		If Sw(okPosition) = Off Then
			Wait Sw(okPosition) = On, 15
		EndIf
		'error handler overtime
		If (Sw(okPosition) = Off) Then
			Print "OKPosition not at ok after 15 second"
		EndIf
		P_Pallet = Pallet(1, Count - 2) +Z(-60 * (Layer - 1)) +X(X_offset * (Layer - 1)) +Y(Y_offset * (Layer - 1))
	ElseIf (Count <= 18) Then
		If Sw(secondPlaceOn) = On Then
			Off secondPlaceReq
			Wait 0.5
			Wait Sw(secondPlaceOn) = Off, 15
		EndIf
		If Sw(okPosition) = Off Then
			Wait Sw(okPosition) = On, 15
		EndIf
		'error handler overtime
		If (Sw(okPosition) = Off) Then
			Print "OKPosition not at ok after 15 second"
		EndIf
		P_Pallet = Pallet(2, Count - 10) +Z(-60 * (Layer - 1)) +X(X_offset * (Layer - 1)) +Y(Y_offset * (Layer - 1))

	ElseIf (Count <= 22) Then
		If Sw(secondPlaceOn) = Off Then
			On secondPlaceReq
			Wait 0.5
			Wait Sw(secondPlaceOn) = On, 15
		EndIf
		If Sw(okPosition) = Off Then
			Wait Sw(okPosition) = On, 15
		EndIf
		'error handler overtime
		If (Sw(okPosition) = Off) Then
			Print "OKPosition not at ok after 15 second"
		EndIf
		If (Count = 19) Then
			P_Pallet = L1 +Z(-60 * (Layer - 1)) +X(X_offset * (Layer - 1)) +Y(Y_offset * (Layer - 1))
		ElseIf (Count = 20) Then
			P_Pallet = L2 +Z(-60 * (Layer - 1)) +X(X_offset * (Layer - 1)) +Y(Y_offset * (Layer - 1))
		ElseIf (Count = 21) Then
			P_Pallet = L3 +Z(-60 * (Layer - 1)) +X(X_offset * (Layer - 1)) +Y(Y_offset * (Layer - 1))
		ElseIf (Count = 22) Then
			P_Pallet = L4 +Z(-60 * (Layer - 1)) +X(X_offset * (Layer - 1)) +Y(Y_offset * (Layer - 1))
		EndIf

	ElseIf (Count = 23) Then
		If Sw(secondPlaceOn) = Off Then
			On secondPlaceReq
			Wait 0.5
			Wait Sw(secondPlaceOn) = On, 15
		EndIf
		If Sw(okPosition) = Off Then
			Wait Sw(okPosition) = On, 15
		EndIf
		'error handler overtime
		If (Sw(okPosition) = Off) Then
			Print "OKPosition not at ok after 15 second"
		EndIf
		P_Pallet = Last +Z(-60 * (Layer - 1)) +X(X_offset * (Layer - 1)) +Y(Y_offset * (Layer - 1))
	Else
		Print "Count error"
	EndIf
	MemOn IsPalletOk
Fend
'true when layer does not match
Function IsLayerChanged As Boolean
	Integer B0, B1, B2, NLayer
	Print "Old Layer:", Layer
	If (Sw(plateBit0) = On) Then
		B0 = 1
	Else
		B0 = 0
	EndIf
	If (Sw(plateBit1) = On) Then
		B1 = 2
	Else
		B1 = 0
	EndIf
	If (Sw(plateBit2) = On) Then
		B2 = 4
	Else
		B2 = 0
	EndIf
	NLayer = B0 + B1 + B2
	If (NLayer = Layer) Then
		IsLayerChanged = False
	Else
		IsLayerChanged = True
		Print "Layer changed. New Layer:", NLayer
	EndIf
Fend
	
Function UpdateLayer
	Integer B0, B1, B2, NLayer
	If (Sw(plateBit0)) = On Then
		B0 = 1
		On plateConfirmBit0
	Else
		B0 = 0
		Off plateConfirmBit0
	EndIf
	If (Sw(plateBit1)) = On Then
		B1 = 2
		On plateConfirmBit1
	Else
		B1 = 0
		Off plateConfirmBit1
	EndIf
	If (Sw(plateBit2)) = On Then
		B2 = 4
		On plateConfirmBit2
	Else
		B2 = 0
		Off plateConfirmBit2
	EndIf

	NLayer = B0 + B1 + B2
	Print "Old Layer:", Layer, " New Layer:", NLayer
	Layer = NLayer
Fend

Function BgMain
	
	TmReset (3)
	TmReset (2)
	Boolean ConvLoc ' lock signal to prevent conveyor move when no part detected
	ConvLoc = False
	Do
	If Sw(528) = True Then
		temp_cnt_hmi = 0
		OutReal 35, temp_cnt_hmi, Forced
	EndIf
	If TaskInfo(main, 3) = -1 Or TaskInfo(main, 3) = 4 Or TaskInfo(main, 3) = 3 Then
		'Off conveyorRun
		Off robotRun, Forced
		Off motroRun, Forced
	Else
		On robotRun, Forced
	EndIf
	'Print "TaskState", TaskInfo(main, 3)
	If Sw(conveyorSensor1) = Off Or Sw(conveyorSensor2) = Off Then
		TmReset (3)
		MemOff IsPickOk
        If ConvLoc = False Then
        	On conveyorRun, Forced
        	If Tmr(2) > 7.1 Then ' Time the conveyor stays on
        		Off conveyorRun, Forced
        		ConvLoc = True
        		TmReset (2)
        	EndIf
        Else
        	Off conveyorRun, Forced
        	If Tmr(2) > 7 Then 'Time the conveyor stays off
        		ConvLoc = False
        		TmReset (2)
        	EndIf
        EndIf
    Else
       	TmReset (2)
       	ConvLoc = False
       	If Tmr(3) <= 2.1 Then 'Time the conveyor stays on when parts presented
       		On conveyorRun, Forced
       		MemOff IsPickOk
       	Else
       		Off conveyorRun, Forced
       		MemOn IsPickOk
       	EndIf
	EndIf
	Wait 0.05
	Loop
Fend
Function updateHmiNumbers
	temp_cnt_hmi = temp_cnt_hmi + 2
	tot_cnt_hmi = tot_cnt_hmi + 2
	OutReal 35, temp_cnt_hmi
	OutReal 37, tot_cnt_hmi
Fend
Function point_shift
	Integer i
	For i = 32 To 47
		P(i) = P(i) +X(0) +Y(0) +U(0.5)
		
	Next
	SavePoints "robot1.pts"
Fend
Function pallet_build
	Pallet 1, PL1, PL2, PL3, PL4, 2, 4
	Pallet 2, pl5, PL6, PL7, PL8, 2, 4
	Pallet 3, PL9, PL10, PL9, 4, 1
Fend
Function test_pallet
	Motor On
	Power_Mid
	Tool 1
	Speed 30
	Accel 15, 15
	SpeedS 400
	AccelS 1000, 1000
	Off vacuumSol1
	Off vacuumSol2
	Go P_Mid LJM
	Count = 1
	'Power_Low
	'Power_High
	Xqt Search_pallet
	Do
		Go T_PF_E_1 CP
		Call Drop_Pallet
		Move P_Pick_Part +Z(125) +X(33.5) CP
		
		Go P_pick_Part1 CP
		Go T_PP_DPH_1 CP
		Xqt Search_pallet
	Loop
Fend
Function cali
	FatalError "CALI_STOP"
	If (CX(Here) > 400) Then
		Pass P_Pick_NP1
		
		Go Here :X(430) LJM
		Go T_PP_DPH_2 LJM
		Go T_PP_DPH_1 LJM
		Go P_Pick_Part1 LJM
	ElseIf (CY(Here) > 400) Then
		Go Here +Z(50) LJM
		Go Here :Y(400) LJM
        Move Here :Z(550)
		Go P_Pick_Part1 LJM
	Else
		Pass Here :Z(550) LJM
		Go P_Pick_Part1 LJM
	EndIf
	Move T_PP_DPH_1
	Move T_PP_DPH_2
	Go P_Pick_NPS +Z(15) LJM
	'Go P_Pick_NP +Z(15) LJM 
	Move T_PP_DPH_2
	Move P_Drop_NP +Z(25)
	Move P_Drop_NP +Z(3.5) +W(-5) +X(2.2);
	Move P_Drop_NP +Z(2.5) +W(-5) +X(4.2);
	Move P_Drop_NP +Z(1.3) +W(-1) +X(2.2) ROT
Fend
