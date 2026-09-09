# TVM802-WEB-API
Web API to control a TVM802(A) Pick and place machine..

Prerequisites
-------------
NodeJS - (https://nodejs.org/en/)

Installation
------------

Download the code and run the following command to install the needed modules 

`npm install`

Running
-------

`node TVM-API-Server.js`

Usage
-----

* http://localhost/ - Test Interface
* http://localhost/api/status - Machine Status
* http://localhost/api/set?XXXX=XXXX - Control interface

Examples:

* http://localhost/api/set?buzzer=1 - Buzzer on
* http://localhost/api/set?buzzer=0 - Buzzer off
* http://localhost/api/set?prick=1
* http://localhost/api/set?pump=1
* http://localhost/api/set?blowing1=1
* http://localhost/api/set?blowing2=1
* http://localhost/api/set?vacuum1=1
* http://localhost/api/set?vacuum2=1
* http://localhost/api/set?leds=1 - Up facing camera leds
