#define TRAY_HEIGHT 2.5
#define LAST_HEIGHT -91
Integer currentTray, Count, NPCount
Boolean isCalib
Real trayZ

Function main
    
	Xqt Feeder_Control
    Motor On
    Call Power_Low
	Call Init
	Call Power_High
    
	Do
        Call T6_Cycle
	Loop
Fend


'=======================

'=======================
Function Power_High
    Power High
    Speed 100
    Accel 120, 120
    SpeedS 2000
    AccelS 10000, 10000
Fend

Function Power_Low
    Power Low
    Speed 10
    Accel 10, 10
    SpeedS 80
    AccelS 200, 200
Fend


'=======================

'=======================
Function Init
	Off dropNameplateReq
	Off gripperSol
	Off pickPartReq
	Off putPartReq
	Off stamNGlueReq
	Off stampReq
	Off tray1EjectReq
	Off tray2EjectReq
	Off vacuumSol1
	Off vacuumSol2
	Off escapmentLoad
	Off escapmentPick
	MemOff subCycleOn
	currentTray = 0
	Count = 0
	NPCount = 0
	isCalib = False
	trayZ = 0
	'check gripper
	
	On gripperSol; Wait 0.25
	If Sw(gripperSensor1) = On Then
		Print "Check Gripper1"
		Quit All
	EndIf
	Off gripperSol; Wait 0.25
	If Sw(gripperSensor1) = Off Then
		Print "Check Gripper1"
		Quit All
	EndIf

	On vacuumSol1; Wait 0.25
	If Sw(gripperSensor2) = Off Then
		Print "Check Gripper2"
		Quit All
	EndIf
	Off vacuumSol1; Wait 0.25
		If Sw(gripperSensor2) = On Then
		Print "Check Gripper2"
		Quit All
	EndIf
	'end check gripper
	Wait 5
	Wait Sw(finishedPutPart) = On And Sw(finishedDropNameplate) = On And Sw(finishPickPart) = On
	Go Here :Z(-10)
	If (Agl(2) < 0) Then
		JTran 2, (-110 - Agl(2))
		JTran 1, (38 - Agl(1))
		JTran 2, (-60 - Agl(2))
		JTran 1, (-14 - Agl(1))
		JTran 2, (130 - Agl(2))
		JTran 1, (30 - Agl(1))
	Else
		JTran 2, (130 - Agl(2))
		JTran 1, (22 - Agl(1))
	EndIf

	Go P_mid
	On pickPartReq; On dropNameplateReq; On putPartReq
	Wait Sw(finishedPutPart) = Off And Sw(finishedDropNameplate) = Off And Sw(finishPickPart) = Off
Fend



'=======================
Function T6_Cycle
	
	'----- Nameplate -----
	If (Count >= 15) Then
		Call Dump_Tray
	EndIf
    Call Pick_Nameplate
    TmReset (4)
    Wait MemSw(subCycleOn) = Off
    If (NPCount <= 0) Then
        Call Place_Nameplate
    EndIf
    TmReset (2)
    '----- Inner 1 -----  
    Call Pick_Inner
    Call Place_Inner_1

    '----- Inner 2 -----
    Call Place_Inner_2
    Print Tmr(2)
	'todo check to NP
	Xqt SubCycle
    

Fend
Function T6_Cycle_Test
	Xqt Feeder_Control
	Call Power_Low
	Speed 10
	Accel 100, 100
	SpeedS 100
	AccelS 300, 300
	'Power_High
	Do
	'----- Nameplate -----
	Pause
	TmReset (3)
	If (Count >= 15) Then
		Call Dump_Tray
	EndIf
    Call Pick_Nameplate
    'Wait MemSw(subCycleOn) = Off
    'Pause
    'Pause

        Call Place_Nameplate

        NPCount = 0

    '----- Inner 1 -----
    Call Pick_Inner
    Call Place_Inner_1

    '----- Inner 2 -----
    Call Pick_Inner
    Call Place_Inner_2
    Print Tmr(3)
	'todo check to NP
	
    Loop

Fend
Function SubCycle
	MemOn subCycleOn
	Call Request_Put_part

    '----- Stamp + Glue -----
    Call Request_Stamp_Glue

    '----- VT6 ?? Nameplate(? fixture ???) -----
    Call Request_Put_Nameplate

    '----- Stamp only -----
    Call Request_Stamp

    '----- VT6 ???? part -----
    Call Request_Take_parts
    MemOff subCycleOn
Fend
Function SubCycleTest
	
	On putPartReq; On pickPartReq; Off dropNameplateReq
	Pause
	MemOn subCycleOn
	Call Request_Put_part

    '----- Stamp + Glue -----
    Call Request_Stamp_Glue

    '----- VT6 ?? Nameplate(? fixture ???) -----
    Call Request_Put_Nameplate

    '----- Stamp only -----
    Call Request_Stamp

    '----- VT6 ???? part -----
    Call Request_Take_parts
    MemOff subCycleOn
    
Fend

'=======================

'=======================
Function Pick_Inner

    'Go T_DNP_PI_1 CP ! D85; On putPartReq; On pickPartReq; Off dropNameplateReq; !
    Pass T_DNP_PI_1, On putPartReq, On pickPartReq, Off dropNameplateReq
    'special signal: go to hold location

    'Go T_DNP_PI_2 CP
    Pass T_DNP_PI_2
    'Go P_Inner_Pick +Z(30) +X(30) CP
    Pass P_Inner_Pick +Z(30) +X(30)
    'Go P_Inner_Pick +Z(30) CP
    Pass P_Inner_Pick +Z(30)
    If MemSw(feederOk) = Off Then
		Wait MemSw(feederOk) = On
	EndIf
	If Sw(gripperSensor1) = Off Then
		Wait Sw(gripperSensor1) = On
	EndIf
	Go P_Inner_Pick
    On gripperSol
    Wait 0.2
    
	'Go P_Inner_Pick +Z(30) CP
	Pass P_Inner_Pick +Z(30), MemOff feederOk
	'Go P_Inner_Pick +Z(30) +X(30) CP
	Pass P_Inner_Pick +Z(30) +X(50), On vacuumSol1
	Pass P_Inner_PickS +Z(30) +X(50)
	Pass P_Inner_PickS +Z(30)
	If MemSw(feederOk) = Off Then
		Wait MemSw(feederOk) = On
	EndIf
	If Sw(gripperSensor2) = Off Then
		Wait Sw(gripperSensor2) = On
	EndIf
	Go P_Inner_PickS
    Off vacuumSol1
    Wait 0.2
    Pass P_Inner_PickS +Z(30), MemOff feederOk
    Pass P_Inner_PickS +Z(30) +X(50)
    If Sw(gripperSensor2) = Off Then
		Wait Sw(gripperSensor2) = On
	EndIf
	If Sw(gripperSensor1) = Off Then
		Wait Sw(gripperSensor1) = On
	EndIf

Fend
Function Pick_Inner_test
	Xqt Feeder_Control
	Pick_Inner
	Place_Inner_1
	Place_Inner_2
Fend

Function Place_Inner_1

    'Go T_PI_DI_1 CP
    Pass T_PI_DI_1
	'Pass T_PI_DI_2
	'Go P_Inner1_Place +Z(25) CP
	Pass P_Inner1_Place +Z(25)
	If Sw(fixtureAtOk) = Off Then
		Print "Fixture not at ok, check Arduino 2 status!"
		Quit All
	EndIf
	If Sw(gripperSensor1) = Off Then
		Print "check gripper1"
		Quit All
	EndIf
	Go P_Inner1_Place ! D85; Off gripperSol !
    Wait 0.15
	NPCount = NPCount - 1
	Pass Here +Z(25)
'    Jump P_mid

Fend

Function Place_Inner_2


	'Go T_PI_DI_1 CP
	'Pass T_PI_DI_1
	'Pass T_PI_DI_2
	'Go P_Inner2_Place +Z(25) CP
	Pass P_Inner2_Place +Z(25)
	If Sw(fixtureAtOk) = Off Then
		Print "Fixture not at ok, check Arduino 2 status!"
		Quit All
	EndIf
	If Sw(gripperSensor2) = Off Then
		Print "check gripper2"
		Quit All
	EndIf
	Go P_Inner2_Place ! D85; On vacuumSol1 !
	Wait 0.1
	'Go Here +Z(25) CP
	Pass Here +Z(25), Off vacuumSol1
	'Go T_DI_PNP_1 CP
	Pass T_DI_PNP_1
	NPCount = NPCount - 1
    
	
Fend


'=======================
' ? / ? Nameplate
'=======================
Function Pick_Nameplate
	If (NPCount <= 0) Then
	Pass T_Mid_PNP_1
	Pass T_Mid_PNP_2
	Call Search_Nameplate
		If (Count <= 6) Then
			If (Oport(tray1EjectReq) = On) Then
				Off tray1EjectReq
				Wait 1
			EndIf
			If (Sw(nameplateTray2AtOk) = Off) Then
				Wait Sw(nameplateTray2AtOk) = On
			EndIf
			Go P_NP_Pick +Z(25)
    		Go P_NP_Pick
			On vacuumSol1
    		On vacuumSol2
    		Wait 0.5
			Count = Count + 1
    		Pass Here +Z(25)
		ElseIf (Count = 7) Then
			If (Oport(tray1EjectReq) = On) Then
				Off tray1EjectReq
				Wait 1
			EndIf
			If (Sw(nameplateTray2AtOk) = Off) Then
				Wait Sw(nameplateTray2AtOk) = On
			EndIf
    		Go P_NP_Pick +Z(25)
    		Go P_NP_Pick
			On vacuumSol1
    		Wait 0.5
    		Pass Here +Z(25)
    		Go P_NP_PickS +Z(25) +Y(-40); Go P_NP_PickS +Z(10) +Y(-40); Off vacuumSol1; Wait 0.7; Pass Here +Z(25)
    		
    		Go P_NP_PickS +Z(25)
    		Go P_NP_PickS
			On vacuumSol1; On vacuumSol2
    		Wait 0.5
    		Count = Count + 1
    		Pass Here +Z(25)
			
			On tray1EjectReq
		ElseIf (Count <= 13) Then
			If (Oport(tray1EjectReq) = Off) Then
				On tray1EjectReq
				Wait 1
			EndIf
			If (Sw(nameplateTray2AtOk) = Off) Then
				Wait Sw(nameplateTray2AtOk) = On
			EndIf
			Go P_NP_Pick +Z(25)
    		Go P_NP_Pick
			On vacuumSol1
    		On vacuumSol2
    		Wait 0.5
			Count = Count + 1
    		Pass Here +Z(25)
		ElseIf (Count = 14) Then
			If (Oport(tray1EjectReq) = Off) Then
				On tray1EjectReq
				Wait 1
			EndIf
			If (Sw(nameplateTray2AtOk) = Off) Then
				Wait Sw(nameplateTray2AtOk) = On
			EndIf
			
    		Go P_NP_Pick +Z(25)
    		Go P_NP_Pick
			On vacuumSol1
			
    		Wait 0.5
    		Pass Here +Z(25)
    		Go P_NP_PickS +Z(25) +Y(-40); Go P_NP_PickS +Z(10) +Y(-40); Off vacuumSol1; Wait 0.7; Pass Here +Z(25)
    		Go P_NP_PickS +Z(25)
    		Go P_NP_PickS
			On vacuumSol1; On vacuumSol2
    		Wait 0.5
    		Count = Count + 1
    		Pass Here +Z(25)
    		
    		Off tray1EjectReq
		EndIf
		Pass T_PNP_DNP_1
	EndIf
    Pass T_PNP_DNP_2

    

Fend
Function Pick_Nameplate2


    Jump P_NP_Pick +Y(40)

    On vacuumSol2
    Wait 0.5

    Jump P_mid

Fend

Function Place_Nameplate


    Go P_NameplateFixture1_Place +Z(25)
	Go P_NameplateFixture1_Place ! D50; Off vacuumSol1 !
	If Tmr(4) > 30 Then
		Wait 1
	EndIf
    Wait 0.2
	
    Go P_NameplateFixture2_Place +Z(25)
    Go P_NameplateFixture2_Place ! D50; Off vacuumSol2 !
    If Tmr(4) > 30 Then
		Wait 1
	EndIf
    Wait 0.2
    NPCount = 4
    Pass Here :Z(-25)
Fend


'=======================
' Dump Tray(???? dump ?? tray)
'=======================
Function Dump_Tray
	Integer i
	Pass T_Mid_PNP_1
	Pass T_Mid_PNP_2
	If (currentTray = 1) Then
		Jump Pallet(1, 4) :Z(trayZ - 3) +Y(40)
		On vacuumSol1
		On vacuumSol2
		Wait 0.5
		Power_Low
		Go Here +Z(25)
		Power_High
		For i = 0 To 15
			Move Here +Z(-8)
			Move Here +Z(8)
		Next
		Go Here +Z(15)
		Wait 0.5
		Power_High
		Speed 60
		Accel 30, 30
		Jump P_Tray_Dump2 ' todo: set dump
		Off vacuumSol1
		Off vacuumSol2
		Wait 0.3
		Count = 1
		trayZ = trayZ - TRAY_HEIGHT
		If (trayZ < LAST_HEIGHT) Then 'todo set height limit
			On tray2EjectReq
			Wait Sw(nameplateTray2AtOk) = Off
			Off tray2EjectReq
		EndIf
		Power_High
		Pass T_Mid_PNP_2
		
	ElseIf (currentTray = 2) Then
		Jump Pallet(3, 4) :Z(trayZ - 3) +Y(40)
		On vacuumSol1
		On vacuumSol2
		Wait 0.5
		Power_Low
		Go Here +Z(25)
		Power_High
		For i = 0 To 15
			Move Here +Z(-8)
			Move Here +Z(8)
		Next
		Go Here +Z(15)
		Wait 0.5
		Power_High
		Speed 60
		Accel 30, 30

		Jump P_Tray_Dump2 ' todo: set dump
		Off vacuumSol1
		Off vacuumSol2
		Wait 0.3
		Count = 1
		trayZ = trayZ - TRAY_HEIGHT
		If (trayZ < LAST_HEIGHT) Then
			On tray2EjectReq
			Wait Sw(nameplateTray2AtOk) = Off
			Off tray2EjectReq
		EndIf
		Power_High
		Pass T_Mid_PNP_2
	EndIf
Fend


'=======================
' ?? Nameplate(?? / ???,??)
'=======================
Function Search_Nameplate
	Do
	If (currentTray = 1 And Sw(nameplateTray1AtOk) = Off And Sw(nameplateTray2AtOk) = On) Then
		Exit Do
	ElseIf (currentTray = 2 And Sw(nameplateTray1AtOk) = On And Sw(nameplateTray2AtOk) = On) Then
		Exit Do
	ElseIf (Sw(nameplateTray1AtOk) = Off And Sw(nameplateTray2AtOk) = On) Then
		currentTray = 1
		Count = 1
		isCalib = False
		Exit Do
	ElseIf (Sw(nameplateTray1AtOk) = On And Sw(nameplateTray2AtOk) = On) Then
		currentTray = 2
		Count = 1
		isCalib = False
		Exit Do
	Else
		currentTray = 0
		Count = 0
		isCalib = False
		trayZ = 0
	EndIf
	Wait 0.1
	Loop
	If (isCalib = False) Then
		Off tray1EjectReq
		Wait 0.5
		Wait Sw(nameplateTray2AtOk) = On
		If (currentTray = 1) Then
			Jump P_TrayZ1
			SpeedS 15
			Move Here :Z(-60) Till Sw(gripperSensor2) = On
			If TillOn Then
				trayZ = CZ(Here) - 18
				' todo temp trayZ height:
				trayZ = -78
				Print "TrayZ:", trayZ
				isCalib = True
			Else
				Print "fail to search NPtray1"
				trayZ = -78
				isCalib = True
				'Quit All
			EndIf
			'SpeedS 100
			Power_High
		ElseIf (currentTray = 2) Then
			Jump P_TrayZ2
			SpeedS 15
			Move Here :Z(-60) Till Sw(gripperSensor2) = On
			If TillOn Then
				trayZ = CZ(Here) - 18
				' todo temp trayZ height:
				trayZ = -78
				Print "TrayZ:", trayZ
				isCalib = True
			Else
				Print "fail to search NPtray2"
				trayZ = -78
				isCalib = True
				'Quit All
			EndIf
			'SpeedS 120
			Power_High
		EndIf
	EndIf
	 
	If (currentTray = 1) Then
		If (Count <= 0) Then
			Print "count<=0!"
			Quit All
		ElseIf (Count <= 6) Then
            P_NP_Pick = Pallet(1, Count) :Z(trayZ)
		ElseIf (Count = 7) Then
			P_NP_Pick = L1 :Z(trayZ)
			P_NP_PickS = L2 :Z(trayZ)
		ElseIf (Count <= 13) Then
		P_NP_Pick = Pallet(2, Count - 7) :Z(trayZ)
		ElseIf (Count = 14) Then
			P_NP_Pick = L3 :Z(trayZ)
			P_NP_PickS = L4 :Z(trayZ)
		ElseIf (Count >= 15) Then
			Print "count>=15!"
			Quit All
		EndIf
		
	ElseIf (currentTray = 2) Then
		If (Count <= 0) Then
			Print "count<=0!"
			Quit All
		ElseIf (Count <= 6) Then
            P_NP_Pick = Pallet(3, Count) :Z(trayZ)
		ElseIf (Count = 7) Then
			P_NP_Pick = R1 :Z(trayZ)
			P_NP_PickS = R2 :Z(trayZ)
		ElseIf (Count <= 13) Then
		P_NP_Pick = Pallet(4, Count - 7) :Z(trayZ)
		ElseIf (Count = 14) Then
			P_NP_Pick = R3 :Z(trayZ)
			P_NP_PickS = R4 :Z(trayZ)
		ElseIf (Count >= 15) Then
			Print "count>=15!"
			Quit All
		EndIf
		
	EndIf
	
Fend


'=======================
' ? VT6 / Arduino2 ?????
'=======================

'------------------------------------
' 1. ?? VT6 ? part (put part request)
'    - ??????
'    - On putPartReq
'    - Wait finishedPutPart On
'    - Off putPartReq
'------------------------------------
Function Request_Put_part

    'Jump P_Safe

    On putPartReq
    Off dropNameplateReq
    Off pickPartReq
    Wait Sw(finishedPutPart) = On And Sw(finishedDropNameplate) = Off And Sw(finishPickPart) = Off

    Off putPartReq
    ' ????,???????? Off
    'Wait Sw(finishedPutPart) = Off

Fend


'------------------------------------
' 2. ?? Stamp + Glue (? Arduino_2)
'    - On stampGlueReq
'    - Wait fixtureOk On(fixture ? OK ??,?? glue)
'    - Off stampGlueReq
'------------------------------------
Function Request_Stamp_Glue

    'Jump P_StampSafe

    On stamNGlueReq
    TmReset (1)
    Wait Sw(fixtureAtOk) = Off
    Off stamNGlueReq
    Wait Sw(fixtureAtOk) = On
    Print Tmr(1)
    
    'Wait Sw(fixtureOk) = Off

Fend


'------------------------------------
' 3. ?? VT6 ?? Nameplate
'    T6 ??:dropNameplateReq
'    VT6 ???:finishPickNameplate = On
'------------------------------------
Function Request_Put_Nameplate

    'Jump P_Nameplate_RequestSafe

    Off putPartReq
    On dropNameplateReq
    Off pickPartReq
	Wait Sw(finishedPutPart) = Off And Sw(finishedDropNameplate) = On And Sw(finishPickPart) = Off
    Off dropNameplateReq
    'Wait Sw(finishPickNameplate) = Off

Fend


'------------------------------------
' 4. ?? Stamp only (? Arduino_2)
'    - On stampReq
'    - Wait fixtureOk On(Stamp ??)
'    - Off stampReq
'------------------------------------
Function Request_Stamp

    'Jump P_StampSafe

    On stampReq
    TmReset (1)
    Wait Sw(fixtureAtOk) = Off
    Off stampReq
    Wait Sw(fixtureAtOk) = On
    Print Tmr(1)
    'Wait Sw(fixtureOk) = Off

Fend


'------------------------------------
' 5. ?? VT6 ???? part
'    T6 ??:pickPartReq
'    VT6 ???:finishedPickPart = On
'------------------------------------
Function Request_Take_parts

    'Jump P_PickPart_RequestSafe

    On pickPartReq
    Off putPartReq
    Off dropNameplateReq
    Wait Sw(finishedPutPart) = Off And Sw(finishedDropNameplate) = Off And Sw(finishPickPart) = On

    Off pickPartReq
    'Wait Sw(finishedPickPart) = Off

Fend


'=======================

'=======================
Function Feeder_Control

    Do
        If Sw(inlineFeederSensor) = On Then
        	Wait 0.1
            On escapmentPick
            Off escapmentLoad
            Wait Sw(FeederAtPick) = On
            MemOn feederOk
            Wait MemSw(feederOk) = Off
            Wait Sw(inlineFeederSensor) = Off, 1.5
            If TW = True Then
            	Print "Inner not Picked up"
            	Pause
            	Quit All
            EndIf
        Else
            Off escapmentPick
            On escapmentLoad
            MemOff feederOk
            Wait Sw(FeederAtLoad) = On
        EndIf

        ' ?????? CPU,??????
        Wait 0.1
    Loop

Fend
Function point_shift
	Integer i
	For i = 0 To 0
		P(i) = P(i) +X(0) +Y(0) :U(-88)
	Next
	SavePoints "robot1.pts"
Fend
Function point_load
	Integer i, j, k
	i = 0
	j = 31
	k = 61
	For i = 0 To 23
		P(55) = P(k + i)
		P(k + i) = P(j + i)
		P(j + i) = P(55)
	Next
	SavePoints "robot1.pts"
Fend
Function pallet_build
	Pallet 1, PL11, PL12, PL13, PL14, 2, 3
	Pallet 2, PL15, PL16, PL17, PL18, 2, 3
	Pallet 3, PL21, PL22, PL23, PL24, 2, 3
	Pallet 4, PL25, PL26, PL27, PL28, 2, 3
	
Fend
Function NPtransfer
	Power_High
	Integer i
	For i = 1 To 4
		Jump Pallet(1, i) :Z(-90)
		On vacuumSol1; On vacuumSol2;
		Wait 0.3
		Jump Pallet(3, i) :Z(-80)
		Off vacuumSol1
		Off vacuumSol2
		Wait 0.3
	Next
	For i = 1 To 8
		Jump Pallet(2, i) :Z(-90)
		On vacuumSol1; On vacuumSol2;
		Wait 0.3
		Jump Pallet(4, i) :Z(-80)
		Off vacuumSol1
		Off vacuumSol2
		Wait 0.3
	Next
	Jump L1 :Z(-90)
	On vacuumSol1; Wait 0.3
	Jump L2 :Z(-90)
	On vacuumSol2; Wait 0.3
	Jump R2 :Z(-80)
	Off vacuumSol1; Wait 0.3
	Jump R1 :Z(-80)
	Off vacuumSol2; Wait 0.3
	Jump L3 :Z(-90)
	On vacuumSol1; Wait 0.3
	Jump L4 :Z(-90)
	On vacuumSol2; Wait 0.3
	Jump R4 :Z(-80)
	Off vacuumSol1; Wait 0.3
	Jump R3 :Z(-80)
	Off vacuumSol2; Wait 0.3
	Go Here :Z(-30)
Fend
Function test
	Integer i
	Power_Low
	Go Here :Z(-40)
		Power_High
		TmReset (4)
		For i = 0 To 15
			Move Here +Z(-7)
			Move Here +Z(7)
		Next
		Print Tmr(4)
Fend

