(function(root){const manifest={
  "name": "Arthur web avatar",
  "version": 1,
  "width": 192,
  "height": 208,
  "neutralAction": "idle",
  "atlas": {
    "image": "arthur-spritesheet.png",
    "width": 1536,
    "height": 2288
  },
  "actions": {
    "idle": {
      "image": "strips/idle.png",
      "frames": [
        0,
        1,
        2,
        3,
        4,
        5
      ],
      "durationsMs": [
        280,
        110,
        110,
        140,
        140,
        320
      ],
      "loop": true,
      "sourceAvatarState": "idle"
    },
    "move-right": {
      "image": "strips/move-right.png",
      "frames": [
        0,
        1,
        2,
        3,
        4,
        5,
        6,
        7
      ],
      "durationsMs": [
        120,
        120,
        120,
        120,
        120,
        120,
        120,
        220
      ],
      "loop": true,
      "sourceAvatarState": "running-right"
    },
    "move-left": {
      "image": "strips/move-left.png",
      "frames": [
        0,
        1,
        2,
        3,
        4,
        5,
        6,
        7
      ],
      "durationsMs": [
        120,
        120,
        120,
        120,
        120,
        120,
        120,
        220
      ],
      "loop": true,
      "sourceAvatarState": "running-left"
    },
    "greeting": {
      "image": "strips/greeting.png",
      "frames": [
        0,
        1,
        2,
        3
      ],
      "durationsMs": [
        140,
        140,
        140,
        280
      ],
      "loop": true,
      "sourceAvatarState": "waving"
    },
    "celebrate": {
      "image": "strips/celebrate.png",
      "frames": [
        0,
        1,
        2,
        3,
        4
      ],
      "durationsMs": [
        140,
        140,
        140,
        140,
        280
      ],
      "loop": true,
      "sourceAvatarState": "jumping"
    },
    "problem": {
      "image": "strips/problem.png",
      "frames": [
        0,
        1,
        2,
        3,
        4,
        5,
        6,
        7
      ],
      "durationsMs": [
        140,
        140,
        140,
        140,
        140,
        140,
        140,
        240
      ],
      "loop": true,
      "sourceAvatarState": "failed"
    },
    "waiting": {
      "image": "strips/waiting.png",
      "frames": [
        0,
        1,
        2,
        3,
        4,
        5
      ],
      "durationsMs": [
        150,
        150,
        150,
        150,
        150,
        260
      ],
      "loop": true,
      "sourceAvatarState": "waiting"
    },
    "thinking": {
      "image": "strips/thinking.png",
      "frames": [
        0,
        1,
        2,
        3,
        4,
        5
      ],
      "durationsMs": [
        120,
        120,
        120,
        120,
        120,
        220
      ],
      "loop": true,
      "sourceAvatarState": "running"
    },
    "reading": {
      "image": "strips/reading.png",
      "frames": [
        0,
        1,
        2,
        3,
        4,
        5
      ],
      "durationsMs": [
        150,
        150,
        150,
        150,
        150,
        280
      ],
      "loop": true,
      "sourceAvatarState": "review"
    },
    "typing": {
      "image": "strips/typing.png",
      "frames": [
        0,
        1,
        2,
        3,
        4,
        5
      ],
      "durationsMs": [
        150,
        150,
        150,
        150,
        150,
        150
      ],
      "loop": true,
      "sourceAvatarState": null
    },
    "searching": {
      "image": "strips/searching.png",
      "frames": [
        0,
        1,
        2,
        3,
        4,
        5
      ],
      "durationsMs": [
        150,
        150,
        150,
        150,
        150,
        150
      ],
      "loop": true,
      "sourceAvatarState": null
    },
    "sleeping": {
      "image": "new-sheets/sleeping.png",
      "frames": [
        0,
        1,
        2,
        3,
        4,
        5
      ],
      "durationsMs": [
        360,
        320,
        320,
        320,
        320,
        360
      ],
      "loop": false,
      "label": "Sleeping",
      "trigger": "No work in flight for 30+ minutes, or night on the orb clock",
      "sourceSize": {
        "width": 2172,
        "height": 724
      },
      "frameRects": [
        {
          "x": 30,
          "y": 183,
          "width": 308,
          "height": 357
        },
        {
          "x": 392,
          "y": 183,
          "width": 308,
          "height": 357
        },
        {
          "x": 754,
          "y": 183,
          "width": 308,
          "height": 357
        },
        {
          "x": 1116,
          "y": 183,
          "width": 308,
          "height": 357
        },
        {
          "x": 1478,
          "y": 183,
          "width": 308,
          "height": 357
        },
        {
          "x": 1840,
          "y": 183,
          "width": 308,
          "height": 357
        }
      ],
      "destinationRect": {
        "x": 6.2745098039215605,
        "y": 0,
        "width": 179.45098039215688,
        "height": 208
      }
    },
    "stamping": {
      "image": "new-sheets/stamping.png",
      "frames": [
        0,
        1,
        2,
        3,
        4,
        5
      ],
      "durationsMs": [
        220,
        120,
        100,
        140,
        180,
        320
      ],
      "loop": false,
      "label": "Stamping",
      "trigger": "A proof gets sealed",
      "sourceSize": {
        "width": 2172,
        "height": 724
      },
      "frameRects": [
        {
          "x": 18,
          "y": 166,
          "width": 305,
          "height": 388
        },
        {
          "x": 380,
          "y": 166,
          "width": 305,
          "height": 388
        },
        {
          "x": 742,
          "y": 166,
          "width": 305,
          "height": 388
        },
        {
          "x": 1104,
          "y": 166,
          "width": 305,
          "height": 388
        },
        {
          "x": 1466,
          "y": 166,
          "width": 305,
          "height": 388
        },
        {
          "x": 1828,
          "y": 166,
          "width": 305,
          "height": 388
        }
      ],
      "destinationRect": {
        "x": 14.24742268041237,
        "y": 0,
        "width": 163.50515463917526,
        "height": 208
      }
    },
    "raising_hand": {
      "image": "new-sheets/raising_hand.png",
      "frames": [
        0,
        1,
        2,
        3,
        4,
        5
      ],
      "durationsMs": [
        220,
        180,
        180,
        180,
        180,
        260
      ],
      "loop": false,
      "label": "Raising a hand",
      "trigger": "A record waits on the user: question batch, unsigned brief or open order",
      "sourceSize": {
        "width": 2172,
        "height": 724
      },
      "frameRects": [
        {
          "x": 0,
          "y": 93,
          "width": 362,
          "height": 522
        },
        {
          "x": 362,
          "y": 93,
          "width": 362,
          "height": 522
        },
        {
          "x": 724,
          "y": 93,
          "width": 362,
          "height": 522
        },
        {
          "x": 1086,
          "y": 93,
          "width": 362,
          "height": 522
        },
        {
          "x": 1448,
          "y": 93,
          "width": 362,
          "height": 522
        },
        {
          "x": 1810,
          "y": 93,
          "width": 362,
          "height": 522
        }
      ],
      "destinationRect": {
        "x": 23.877394636015325,
        "y": 0,
        "width": 144.24521072796935,
        "height": 208
      }
    },
    "juggling": {
      "image": "new-sheets/juggling.png",
      "frames": [
        0,
        1,
        2,
        3,
        4,
        5
      ],
      "durationsMs": [
        150,
        150,
        150,
        150,
        150,
        150
      ],
      "loop": false,
      "label": "Juggling",
      "trigger": "Eight or more distinct tasks/jobs at once",
      "sourceSize": {
        "width": 2172,
        "height": 724
      },
      "frameRects": [
        {
          "x": 5,
          "y": 103,
          "width": 353,
          "height": 534
        },
        {
          "x": 367,
          "y": 103,
          "width": 353,
          "height": 534
        },
        {
          "x": 729,
          "y": 103,
          "width": 353,
          "height": 534
        },
        {
          "x": 1091,
          "y": 103,
          "width": 353,
          "height": 534
        },
        {
          "x": 1453,
          "y": 103,
          "width": 353,
          "height": 534
        },
        {
          "x": 1815,
          "y": 103,
          "width": 353,
          "height": 534
        }
      ],
      "destinationRect": {
        "x": 27.250936329588015,
        "y": 0,
        "width": 137.49812734082397,
        "height": 208
      }
    },
    "drinking_coffee": {
      "image": "new-sheets/drinking_coffee.png",
      "frames": [
        0,
        1,
        2,
        3,
        4,
        5
      ],
      "durationsMs": [
        280,
        220,
        240,
        260,
        240,
        360
      ],
      "loop": true,
      "label": "Drinking coffee",
      "trigger": "A job has run for more than one hour",
      "sourceSize": {
        "width": 2172,
        "height": 724
      },
      "frameRects": [
        {
          "x": 8,
          "y": 127,
          "width": 352,
          "height": 463
        },
        {
          "x": 370,
          "y": 127,
          "width": 352,
          "height": 463
        },
        {
          "x": 732,
          "y": 127,
          "width": 352,
          "height": 463
        },
        {
          "x": 1094,
          "y": 127,
          "width": 352,
          "height": 463
        },
        {
          "x": 1456,
          "y": 127,
          "width": 352,
          "height": 463
        },
        {
          "x": 1818,
          "y": 127,
          "width": 352,
          "height": 463
        }
      ],
      "destinationRect": {
        "x": 16.933045356371494,
        "y": 0,
        "width": 158.133909287257,
        "height": 208
      }
    },
    "shield_up": {
      "image": "new-sheets/shield_up.png",
      "frames": [
        0,
        1,
        2,
        3,
        4,
        5
      ],
      "durationsMs": [
        220,
        180,
        200,
        220,
        200,
        280
      ],
      "loop": false,
      "label": "Shield up",
      "trigger": "A git guard or gate blocks something",
      "sourceSize": {
        "width": 2172,
        "height": 724
      },
      "frameRects": [
        {
          "x": 0,
          "y": 95,
          "width": 360,
          "height": 531
        },
        {
          "x": 362,
          "y": 95,
          "width": 360,
          "height": 531
        },
        {
          "x": 724,
          "y": 95,
          "width": 360,
          "height": 531
        },
        {
          "x": 1086,
          "y": 95,
          "width": 360,
          "height": 531
        },
        {
          "x": 1448,
          "y": 95,
          "width": 360,
          "height": 531
        },
        {
          "x": 1810,
          "y": 95,
          "width": 360,
          "height": 531
        }
      ],
      "destinationRect": {
        "x": 25.491525423728817,
        "y": 0,
        "width": 141.01694915254237,
        "height": 208
      }
    },
    "counting_coins": {
      "image": "new-sheets/counting_coins.png",
      "frames": [
        0,
        1,
        2,
        3,
        4,
        5
      ],
      "durationsMs": [
        180,
        200,
        200,
        200,
        200,
        320
      ],
      "loop": true,
      "label": "Counting coins",
      "trigger": "A cost spike appears on the burn board",
      "sourceSize": {
        "width": 2172,
        "height": 724
      },
      "frameRects": [
        {
          "x": 52,
          "y": 199,
          "width": 255,
          "height": 354
        },
        {
          "x": 414,
          "y": 199,
          "width": 255,
          "height": 354
        },
        {
          "x": 776,
          "y": 199,
          "width": 255,
          "height": 354
        },
        {
          "x": 1138,
          "y": 199,
          "width": 255,
          "height": 354
        },
        {
          "x": 1500,
          "y": 199,
          "width": 255,
          "height": 354
        },
        {
          "x": 1862,
          "y": 199,
          "width": 255,
          "height": 354
        }
      ],
      "destinationRect": {
        "x": 21.08474576271186,
        "y": 0,
        "width": 149.83050847457628,
        "height": 208
      }
    },
    "packing_box": {
      "image": "new-sheets/packing_box.png",
      "frames": [
        0,
        1,
        2,
        3,
        4,
        5
      ],
      "durationsMs": [
        200,
        160,
        160,
        160,
        180,
        300
      ],
      "loop": false,
      "label": "Packing a box",
      "trigger": "A release candidate is built",
      "sourceSize": {
        "width": 2172,
        "height": 724
      },
      "frameRects": [
        {
          "x": 42,
          "y": 179,
          "width": 264,
          "height": 395
        },
        {
          "x": 404,
          "y": 179,
          "width": 264,
          "height": 395
        },
        {
          "x": 766,
          "y": 179,
          "width": 264,
          "height": 395
        },
        {
          "x": 1128,
          "y": 179,
          "width": 264,
          "height": 395
        },
        {
          "x": 1490,
          "y": 179,
          "width": 264,
          "height": 395
        },
        {
          "x": 1852,
          "y": 179,
          "width": 264,
          "height": 395
        }
      ],
      "destinationRect": {
        "x": 26.491139240506328,
        "y": 0,
        "width": 139.01772151898734,
        "height": 208
      }
    },
    "merging_papers": {
      "image": "new-sheets/merging_papers.png",
      "frames": [
        0,
        1,
        2,
        3,
        4,
        5
      ],
      "durationsMs": [
        200,
        180,
        160,
        180,
        200,
        320
      ],
      "loop": false,
      "label": "Merging papers",
      "trigger": "A branch lands on main",
      "sourceSize": {
        "width": 2172,
        "height": 724
      },
      "frameRects": [
        {
          "x": 49,
          "y": 185,
          "width": 275,
          "height": 352
        },
        {
          "x": 411,
          "y": 185,
          "width": 275,
          "height": 352
        },
        {
          "x": 773,
          "y": 185,
          "width": 275,
          "height": 352
        },
        {
          "x": 1135,
          "y": 185,
          "width": 275,
          "height": 352
        },
        {
          "x": 1497,
          "y": 185,
          "width": 275,
          "height": 352
        },
        {
          "x": 1859,
          "y": 185,
          "width": 275,
          "height": 352
        }
      ],
      "destinationRect": {
        "x": 14.75,
        "y": 0,
        "width": 162.5,
        "height": 208
      }
    },
    "telescope": {
      "image": "new-sheets/telescope.png",
      "frames": [
        0,
        1,
        2,
        3,
        4,
        5
      ],
      "durationsMs": [
        260,
        220,
        220,
        220,
        220,
        320
      ],
      "loop": true,
      "label": "Telescope",
      "trigger": "Roadmap or Mission map is opened",
      "sourceSize": {
        "width": 2172,
        "height": 724
      },
      "frameRects": [
        {
          "x": 12,
          "y": 158,
          "width": 331,
          "height": 396
        },
        {
          "x": 374,
          "y": 158,
          "width": 331,
          "height": 396
        },
        {
          "x": 736,
          "y": 158,
          "width": 331,
          "height": 396
        },
        {
          "x": 1098,
          "y": 158,
          "width": 331,
          "height": 396
        },
        {
          "x": 1460,
          "y": 158,
          "width": 331,
          "height": 396
        },
        {
          "x": 1822,
          "y": 158,
          "width": 331,
          "height": 396
        }
      ],
      "destinationRect": {
        "x": 9.070707070707059,
        "y": 0,
        "width": 173.85858585858588,
        "height": 208
      }
    },
    "spit": {
      "image": "new-sheets/spit.png",
      "frames": [
        0,
        1,
        2,
        3,
        4,
        5
      ],
      "durationsMs": [
        200,
        180,
        100,
        120,
        220,
        420
      ],
      "loop": false,
      "label": "Spit",
      "trigger": "Arthur is clicked ten times rapidly",
      "sourceSize": {
        "width": 2172,
        "height": 724
      },
      "frameRects": [
        {
          "x": 2,
          "y": 86,
          "width": 354,
          "height": 507
        },
        {
          "x": 364,
          "y": 86,
          "width": 354,
          "height": 507
        },
        {
          "x": 726,
          "y": 86,
          "width": 354,
          "height": 507
        },
        {
          "x": 1088,
          "y": 86,
          "width": 354,
          "height": 507
        },
        {
          "x": 1450,
          "y": 86,
          "width": 354,
          "height": 507
        },
        {
          "x": 1812,
          "y": 86,
          "width": 354,
          "height": 507
        }
      ],
      "destinationRect": {
        "x": 23.384615384615387,
        "y": 0,
        "width": 145.23076923076923,
        "height": 208
      }
    },
    "sunglasses": {
      "image": "new-sheets/sunglasses.png",
      "frames": [
        0,
        1,
        2,
        3,
        4,
        5
      ],
      "durationsMs": [
        240,
        200,
        220,
        220,
        200,
        320
      ],
      "loop": false,
      "label": "Sunglasses",
      "trigger": "Switch to light theme",
      "sourceSize": {
        "width": 2172,
        "height": 724
      },
      "frameRects": [
        {
          "x": 0,
          "y": 151,
          "width": 362,
          "height": 441
        },
        {
          "x": 362,
          "y": 151,
          "width": 362,
          "height": 441
        },
        {
          "x": 724,
          "y": 151,
          "width": 362,
          "height": 441
        },
        {
          "x": 1086,
          "y": 151,
          "width": 362,
          "height": 441
        },
        {
          "x": 1448,
          "y": 151,
          "width": 362,
          "height": 441
        },
        {
          "x": 1810,
          "y": 151,
          "width": 362,
          "height": 441
        }
      ],
      "destinationRect": {
        "x": 10.630385487528343,
        "y": 0,
        "width": 170.7392290249433,
        "height": 208
      }
    },
    "nightcap": {
      "image": "new-sheets/nightcap.png",
      "frames": [
        0,
        1,
        2,
        3,
        4,
        5
      ],
      "durationsMs": [
        280,
        240,
        240,
        240,
        240,
        360
      ],
      "loop": true,
      "label": "Nightcap",
      "trigger": "Switch to dark theme",
      "sourceSize": {
        "width": 2170,
        "height": 725
      },
      "frameRects": [
        {
          "x": 18,
          "y": 146,
          "width": 333,
          "height": 449
        },
        {
          "x": 380,
          "y": 146,
          "width": 333,
          "height": 449
        },
        {
          "x": 741,
          "y": 146,
          "width": 333,
          "height": 449
        },
        {
          "x": 1103,
          "y": 146,
          "width": 333,
          "height": 449
        },
        {
          "x": 1465,
          "y": 146,
          "width": 333,
          "height": 449
        },
        {
          "x": 1826,
          "y": 146,
          "width": 333,
          "height": 449
        }
      ],
      "destinationRect": {
        "x": 18.868596881959917,
        "y": 0,
        "width": 154.26280623608017,
        "height": 208
      }
    },
    "yawn_stretch": {
      "image": "new-sheets/yawn_stretch.png",
      "frames": [
        0,
        1,
        2,
        3,
        4,
        5
      ],
      "durationsMs": [
        220,
        180,
        200,
        220,
        220,
        380
      ],
      "loop": false,
      "label": "Yawn and stretch",
      "trigger": "Return to the tab after a long absence",
      "sourceSize": {
        "width": 2172,
        "height": 724
      },
      "frameRects": [
        {
          "x": 10,
          "y": 157,
          "width": 346,
          "height": 423
        },
        {
          "x": 372,
          "y": 157,
          "width": 346,
          "height": 423
        },
        {
          "x": 734,
          "y": 157,
          "width": 346,
          "height": 423
        },
        {
          "x": 1096,
          "y": 157,
          "width": 346,
          "height": 423
        },
        {
          "x": 1458,
          "y": 157,
          "width": 346,
          "height": 423
        },
        {
          "x": 1820,
          "y": 157,
          "width": 346,
          "height": 423
        }
      ],
      "destinationRect": {
        "x": 10.931442080378247,
        "y": 0,
        "width": 170.1371158392435,
        "height": 208
      }
    },
    "mimic_typing": {
      "image": "new-sheets/mimic_typing.png",
      "frames": [
        0,
        1,
        2,
        3,
        4,
        5
      ],
      "durationsMs": [
        150,
        150,
        150,
        150,
        150,
        150
      ],
      "loop": true,
      "label": "Mimic typing",
      "trigger": "Keys are typed in the Ctrl+K command search",
      "sourceSize": {
        "width": 2172,
        "height": 724
      },
      "frameRects": [
        {
          "x": 1,
          "y": 92,
          "width": 360,
          "height": 546
        },
        {
          "x": 363,
          "y": 92,
          "width": 360,
          "height": 546
        },
        {
          "x": 725,
          "y": 92,
          "width": 360,
          "height": 546
        },
        {
          "x": 1087,
          "y": 92,
          "width": 360,
          "height": 546
        },
        {
          "x": 1449,
          "y": 92,
          "width": 360,
          "height": 546
        },
        {
          "x": 1811,
          "y": 92,
          "width": 360,
          "height": 546
        }
      ],
      "destinationRect": {
        "x": 27.42857142857143,
        "y": 0,
        "width": 137.14285714285714,
        "height": 208
      }
    },
    "dance": {
      "image": "new-sheets/dance.png",
      "frames": [
        0,
        1,
        2,
        3,
        4,
        5
      ],
      "durationsMs": [
        180,
        160,
        160,
        160,
        180,
        320
      ],
      "loop": false,
      "label": "Dance",
      "trigger": "Every task in an operation is done",
      "sourceSize": {
        "width": 2172,
        "height": 724
      },
      "frameRects": [
        {
          "x": 29,
          "y": 155,
          "width": 332,
          "height": 424
        },
        {
          "x": 391,
          "y": 155,
          "width": 332,
          "height": 424
        },
        {
          "x": 753,
          "y": 155,
          "width": 332,
          "height": 424
        },
        {
          "x": 1115,
          "y": 155,
          "width": 332,
          "height": 424
        },
        {
          "x": 1477,
          "y": 155,
          "width": 332,
          "height": 424
        },
        {
          "x": 1839,
          "y": 155,
          "width": 332,
          "height": 424
        }
      ],
      "destinationRect": {
        "x": 14.56603773584905,
        "y": 0,
        "width": 162.8679245283019,
        "height": 208
      }
    },
    "party_hat": {
      "image": "new-sheets/party_hat.png",
      "frames": [
        0,
        1,
        2,
        3,
        4,
        5
      ],
      "durationsMs": [
        220,
        180,
        180,
        180,
        220,
        420
      ],
      "loop": false,
      "label": "Party hat",
      "trigger": "A user-chosen date such as release day or birthday",
      "sourceSize": {
        "width": 2172,
        "height": 724
      },
      "frameRects": [
        {
          "x": 0,
          "y": 96,
          "width": 362,
          "height": 527
        },
        {
          "x": 362,
          "y": 96,
          "width": 362,
          "height": 527
        },
        {
          "x": 724,
          "y": 96,
          "width": 362,
          "height": 527
        },
        {
          "x": 1086,
          "y": 96,
          "width": 362,
          "height": 527
        },
        {
          "x": 1448,
          "y": 96,
          "width": 362,
          "height": 527
        },
        {
          "x": 1810,
          "y": 96,
          "width": 362,
          "height": 527
        }
      ],
      "destinationRect": {
        "x": 24.561669829222012,
        "y": 0,
        "width": 142.87666034155598,
        "height": 208
      }
    },
    "headphones": {
      "image": "new-sheets/headphones.png",
      "frames": [
        0,
        1,
        2,
        3,
        4,
        5
      ],
      "durationsMs": [
        240,
        200,
        200,
        200,
        200,
        300
      ],
      "loop": true,
      "label": "Headphones",
      "trigger": "A long research run is in progress",
      "sourceSize": {
        "width": 2172,
        "height": 724
      },
      "frameRects": [
        {
          "x": 5,
          "y": 143,
          "width": 352,
          "height": 465
        },
        {
          "x": 367,
          "y": 143,
          "width": 352,
          "height": 465
        },
        {
          "x": 729,
          "y": 143,
          "width": 352,
          "height": 465
        },
        {
          "x": 1091,
          "y": 143,
          "width": 352,
          "height": 465
        },
        {
          "x": 1453,
          "y": 143,
          "width": 352,
          "height": 465
        },
        {
          "x": 1815,
          "y": 143,
          "width": 352,
          "height": 465
        }
      ],
      "destinationRect": {
        "x": 17.273118279569886,
        "y": 0,
        "width": 157.45376344086023,
        "height": 208
      }
    },
    "detective": {
      "image": "new-sheets/detective.png",
      "frames": [
        0,
        1,
        2,
        3,
        4,
        5
      ],
      "durationsMs": [
        260,
        220,
        220,
        220,
        220,
        340
      ],
      "loop": true,
      "label": "Detective",
      "trigger": "A debug or investigation task is in flight",
      "sourceSize": {
        "width": 2172,
        "height": 724
      },
      "frameRects": [
        {
          "x": 11,
          "y": 125,
          "width": 344,
          "height": 512
        },
        {
          "x": 373,
          "y": 125,
          "width": 344,
          "height": 512
        },
        {
          "x": 735,
          "y": 125,
          "width": 344,
          "height": 512
        },
        {
          "x": 1097,
          "y": 125,
          "width": 344,
          "height": 512
        },
        {
          "x": 1459,
          "y": 125,
          "width": 344,
          "height": 512
        },
        {
          "x": 1821,
          "y": 125,
          "width": 344,
          "height": 512
        }
      ],
      "destinationRect": {
        "x": 26.125,
        "y": 0,
        "width": 139.75,
        "height": 208
      }
    },
    "checklist": {
      "image": "new-sheets/checklist.png",
      "frames": [
        0,
        1,
        2,
        3,
        4,
        5
      ],
      "durationsMs": [
        200,
        180,
        180,
        180,
        180,
        320
      ],
      "loop": false,
      "label": "Checklist",
      "trigger": "A verify-phase task is running",
      "sourceSize": {
        "width": 2172,
        "height": 724
      },
      "frameRects": [
        {
          "x": 363,
          "y": 92,
          "width": 361,
          "height": 508
        },
        {
          "x": 363,
          "y": 92,
          "width": 361,
          "height": 508
        },
        {
          "x": 725,
          "y": 92,
          "width": 361,
          "height": 508
        },
        {
          "x": 1087,
          "y": 92,
          "width": 361,
          "height": 508
        },
        {
          "x": 1449,
          "y": 92,
          "width": 361,
          "height": 508
        },
        {
          "x": 1811,
          "y": 92,
          "width": 361,
          "height": 508
        }
      ],
      "destinationRect": {
        "x": 22.094488188976385,
        "y": 0,
        "width": 147.81102362204723,
        "height": 208
      }
    },
    "peekaboo": {
      "image": "new-sheets/peekaboo.png",
      "frames": [
        0,
        1,
        2,
        3,
        4,
        5
      ],
      "durationsMs": [
        200,
        160,
        180,
        180,
        200,
        340
      ],
      "loop": false,
      "label": "Peekaboo",
      "trigger": "The sidebar scrolls across Arthur's slot after idle",
      "sourceSize": {
        "width": 2172,
        "height": 724
      },
      "frameRects": [
        {
          "x": 9,
          "y": 135,
          "width": 342,
          "height": 439
        },
        {
          "x": 371,
          "y": 135,
          "width": 342,
          "height": 439
        },
        {
          "x": 733,
          "y": 135,
          "width": 342,
          "height": 439
        },
        {
          "x": 1095,
          "y": 135,
          "width": 342,
          "height": 439
        },
        {
          "x": 1457,
          "y": 135,
          "width": 342,
          "height": 439
        },
        {
          "x": 1819,
          "y": 135,
          "width": 342,
          "height": 439
        }
      ],
      "destinationRect": {
        "x": 14.97949886104783,
        "y": 0,
        "width": 162.04100227790434,
        "height": 208
      }
    }
  },
  "aliases": {
    "checking-result": "reading",
    "running-right": "move-right",
    "running-left": "move-left",
    "waving": "greeting",
    "jumping": "celebrate",
    "failed": "problem",
    "running": "thinking",
    "review": "reading"
  },
  "gaze": {
    "origin": {
      "x": 0.5,
      "y": 0.38
    },
    "deadZone": 12,
    "smoothingMs": 90,
    "hysteresisDegrees": 3,
    "frames": [
      {
        "index": 0,
        "angle": 0.0,
        "row": 9,
        "column": 0,
        "x": 0,
        "y": 1872,
        "width": 192,
        "height": 208
      },
      {
        "index": 1,
        "angle": 22.5,
        "row": 9,
        "column": 1,
        "x": 192,
        "y": 1872,
        "width": 192,
        "height": 208
      },
      {
        "index": 2,
        "angle": 45.0,
        "row": 9,
        "column": 2,
        "x": 384,
        "y": 1872,
        "width": 192,
        "height": 208
      },
      {
        "index": 3,
        "angle": 67.5,
        "row": 9,
        "column": 3,
        "x": 576,
        "y": 1872,
        "width": 192,
        "height": 208
      },
      {
        "index": 4,
        "angle": 90.0,
        "row": 9,
        "column": 4,
        "x": 768,
        "y": 1872,
        "width": 192,
        "height": 208
      },
      {
        "index": 5,
        "angle": 112.5,
        "row": 9,
        "column": 5,
        "x": 960,
        "y": 1872,
        "width": 192,
        "height": 208
      },
      {
        "index": 6,
        "angle": 135.0,
        "row": 9,
        "column": 6,
        "x": 1152,
        "y": 1872,
        "width": 192,
        "height": 208
      },
      {
        "index": 7,
        "angle": 157.5,
        "row": 9,
        "column": 7,
        "x": 1344,
        "y": 1872,
        "width": 192,
        "height": 208
      },
      {
        "index": 8,
        "angle": 180.0,
        "row": 10,
        "column": 0,
        "x": 0,
        "y": 2080,
        "width": 192,
        "height": 208
      },
      {
        "index": 9,
        "angle": 202.5,
        "row": 10,
        "column": 1,
        "x": 192,
        "y": 2080,
        "width": 192,
        "height": 208
      },
      {
        "index": 10,
        "angle": 225.0,
        "row": 10,
        "column": 2,
        "x": 384,
        "y": 2080,
        "width": 192,
        "height": 208
      },
      {
        "index": 11,
        "angle": 247.5,
        "row": 10,
        "column": 3,
        "x": 576,
        "y": 2080,
        "width": 192,
        "height": 208
      },
      {
        "index": 12,
        "angle": 270.0,
        "row": 10,
        "column": 4,
        "x": 768,
        "y": 2080,
        "width": 192,
        "height": 208
      },
      {
        "index": 13,
        "angle": 292.5,
        "row": 10,
        "column": 5,
        "x": 960,
        "y": 2080,
        "width": 192,
        "height": 208
      },
      {
        "index": 14,
        "angle": 315.0,
        "row": 10,
        "column": 6,
        "x": 1152,
        "y": 2080,
        "width": 192,
        "height": 208
      },
      {
        "index": 15,
        "angle": 337.5,
        "row": 10,
        "column": 7,
        "x": 1344,
        "y": 2080,
        "width": 192,
        "height": 208
      }
    ]
  },
  "packageVersion": 2,
  "newActionCount": 21
};if(typeof module==="object"&&module.exports)module.exports=manifest;else root.ArthurAssets=manifest;})(globalThis);
