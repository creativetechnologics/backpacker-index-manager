# Backpacker Ranked Batch Plan

- Remaining articles: 25760
- Batch size: 2000
- Batch count: 13
- Excludes already filled guide_v2 pages from staging: 139

## Files

- `backpacker_ranked_batches_remaining_2000.csv`: full ordered list.
- `backpacker_ranked_batches_remaining_2000.jsonl`: full ordered list, one article per line.
- `backpacker_ranked_batch_summary_2000.json`: per-batch summary.

## Ranking Signals

Score favors deterministic canonical destinations, full destination parse strategy, primary traveler relevance, travel-base/canonical signals, higher confidence, usable pages with coordinates/image/Wikidata, enough source length, major city/backpacker keywords. It penalizes countries for current guide layout, airports/topics/routes, low relevance, skip/topic strategies, and tiny outline pages.

## Batch Summary

| Batch | Count | Score range | Avg score | Avg page len | Sample top titles | Kind counts |
|---:|---:|---:|---:|---:|---|---|
| 1 | 2000 | 2400.0-2560.0 | 2416.8 | 44598 | Barcelona; Prague; Buenos Aires; Taipei; Cairo; Madrid; Lisbon; Seoul; Delhi; Kyoto; Long Beach Island; Sequoia and Kings Canyon National Parks | {"city": 1999, "park": 1} |
| 2 | 2000 | 2391.2-2400.0 | 2396.7 | 20118 | Benin City; Kalgoorlie–Boulder; Malang; Steamboat Springs; Vechtstreek; Kano; Kyrenia; Joliette; Gallipoli (Italy); Shawinigan; Dessau; Lishui | {"city": 1999, "neighborhood": 1} |
| 3 | 2000 | 2352.4-2391.2 | 2366.0 | 13377 | Bundi; Capital Region (Maryland); Annascaul; Lyme Regis; Sandwich (Massachusetts); Cullera; Tena; Khotan; Reutte; Lohja; Stockton-on-Tees; Meghalaya | {"city": 1969, "park": 21, "neighborhood": 10} |
| 4 | 2000 | 2342.7-2352.4 | 2347.4 | 11783 | Bridge River Valley; Jurmo; Carndonagh; Gagra; Pitt Meadows; Batu Pahat; Modica; Clacton; Cody; Aurora (Illinois); Barry; Upland | {"city": 2000} |
| 5 | 2000 | 2315.0-2342.7 | 2333.9 | 11453 | Takachiho; Fethard-on-Sea; Diamond Valley; Falmouth (Jamaica); Minamidaito; Odemira; Formosa (Brazil); Bonnyville; Cala Millor; Bonaventure; Vama Veche; Stony Point | {"city": 1826, "park": 127, "neighborhood": 47} |
| 6 | 2000 | 2226.4-2315.0 | 2276.5 | 18011 | Jerusalem/West; Bangkok/Khao San Road; Hyderabad/Central; London/Greenwich; Bangkok/Yaowarat and Phahurat; Manhattan/Upper East Side; Hong Kong/Kowloon; Calgary/City Centre; Saint Petersburg/South; Warsaw/Śródmieście; Oslo/Sentrum; San Francisco/Civic Center-Tenderloin | {"city": 968, "neighborhood": 839, "park": 193} |
| 7 | 2000 | 2148.0-2226.4 | 2189.9 | 5981 | Yoro; San Germán; Bensheim; Paluma; Sisimiut; Columbiana; Wadsworth; Oga; Titisee-Neustadt; Oberwesel; Quezon City/Diliman and Katipunan; Hartola | {"city": 1890, "neighborhood": 71, "park": 39} |
| 8 | 2000 | 2034.7-2148.0 | 2059.5 | 5039 | North Charleston; Vernon (New Jersey); Boki; Stone Harbor; Burney; Western Hamilton County; Kunene; Foster; Cornwall (Connecticut); Sea Ranch; Ikirun; Kalpitiya | {"city": 1902, "park": 72, "neighborhood": 26} |
| 9 | 2000 | 1980.0-2034.7 | 2012.0 | 9984 | Latina; Springville; Vidalia (Georgia); Warren (Michigan); Anaklia; Izola; Arujá; Nilo Peçanha; Annavaram; Ikeshima; Júzcar; Boten | {"city": 1953, "park": 43, "neighborhood": 4} |
| 10 | 2000 | 1742.2-1980.0 | 1862.8 | 3575 | Madagascar; Lithuania; Andorra; Brunei; Luxembourg; Maldives; Fiji; Papikonda National Park; Souss-Massa National Park; Northern Mariana Islands; Queen Elizabeth National Park; Fir of Hotova National Park | {"city": 1869, "park": 101, "neighborhood": 30} |
| 11 | 2000 | 453.4-1742.1 | 907.0 | 8500 | Salem (Missouri); Popoyo; Pearl Lagoon; Liaoyuan; Linden (Guyana); Mormon Lake Village; Nanuet; Douglas (Georgia); Balmazújváros; Rosignano Marittimo; Nea Phokea; Chalatenango | {"unknown": 1301, "city": 544, "town": 89, "village": 16, "county": 10, "park": 8, "city_town_village": 7, "neighborhood": 5} |
| 12 | 2000 | 247.7-453.4 | 354.0 | 3348 | Sant Pol de Mar; Ekom-Nkam Waterfalls; Sühbaatar; Amelia (Ohio); Memphis (Egypt); Nasu; Ak-Suu; Maibara; Probištip; Lisieux; Vienne (city); Clayton (Georgia) | {"unknown": 1965, "town": 10, "region": 9, "city": 3, "island": 2, "national_seashore": 2, "county": 2, "park": 1} |
| 13 | 1760 | -1524.7-247.7 | 125.5 | 2274 | Simunul; Rochester (New Hampshire); Jeongeup; Dallas (Georgia); Macerata; Chamela-Cuixmala Biosphere Reserve; Zuwara; Malilipot; Hot Springs (North Carolina); Khar Zurkhnii Khokh Nuur; Tiruvuru; Florida (Puerto Rico) | {"unknown": 1411, "town": 90, "city": 58, "village": 32, "island": 10, "suburb": 10, "archaeological_site": 9, "other": 8} |

## First 100 Articles

| Rank | Batch | Score | Title | Page len | Kind | Strategy | URL |
|---:|---:|---:|---|---:|---|---|---|
| 1 | 1 | 2560.0 | Barcelona | 108216 | city | full_destination | https://en.wikivoyage.org/wiki/Barcelona |
| 2 | 1 | 2560.0 | Prague | 106287 | city | full_destination | https://en.wikivoyage.org/wiki/Prague |
| 3 | 1 | 2560.0 | Buenos Aires | 105016 | city | full_destination | https://en.wikivoyage.org/wiki/Buenos_Aires |
| 4 | 1 | 2560.0 | Taipei | 104306 | city | full_destination | https://en.wikivoyage.org/wiki/Taipei |
| 5 | 1 | 2560.0 | Cairo | 99597 | city | full_destination | https://en.wikivoyage.org/wiki/Cairo |
| 6 | 1 | 2560.0 | Madrid | 94744 | city | full_destination | https://en.wikivoyage.org/wiki/Madrid |
| 7 | 1 | 2560.0 | Lisbon | 93314 | city | full_destination | https://en.wikivoyage.org/wiki/Lisbon |
| 8 | 1 | 2560.0 | Seoul | 87337 | city | full_destination | https://en.wikivoyage.org/wiki/Seoul |
| 9 | 1 | 2560.0 | Delhi | 79637 | city | full_destination | https://en.wikivoyage.org/wiki/Delhi |
| 10 | 1 | 2560.0 | Kyoto | 62989 | city | full_destination | https://en.wikivoyage.org/wiki/Kyoto |
| 11 | 1 | 2480.0 | Long Beach Island | 38863 | city | full_destination | https://en.wikivoyage.org/wiki/Long_Beach_Island |
| 12 | 1 | 2480.0 | Sequoia and Kings Canyon National Parks | 37226 | city | full_destination | https://en.wikivoyage.org/wiki/Sequoia_and_Kings_Canyon_National_Parks |
| 13 | 1 | 2480.0 | Chaco Culture National Historical Park | 22834 | city | full_destination | https://en.wikivoyage.org/wiki/Chaco_Culture_National_Historical_Park |
| 14 | 1 | 2477.8 | Lyndon B. Johnson National Historical Park | 20331 | city | full_destination | https://en.wikivoyage.org/wiki/Lyndon_B._Johnson_National_Historical_Park |
| 15 | 1 | 2470.0 | Newport (Rhode Island) | 80555 | city | full_destination | https://en.wikivoyage.org/wiki/Newport_(Rhode_Island) |
| 16 | 1 | 2470.0 | Long Beach | 77334 | city | full_destination | https://en.wikivoyage.org/wiki/Long_Beach |
| 17 | 1 | 2470.0 | New Smyrna Beach | 68279 | city | full_destination | https://en.wikivoyage.org/wiki/New_Smyrna_Beach |
| 18 | 1 | 2470.0 | Easter Island | 64683 | city | full_destination | https://en.wikivoyage.org/wiki/Easter_Island |
| 19 | 1 | 2470.0 | Diani Beach | 61174 | city | full_destination | https://en.wikivoyage.org/wiki/Diani_Beach |
| 20 | 1 | 2470.0 | Daytona Beach | 60621 | city | full_destination | https://en.wikivoyage.org/wiki/Daytona_Beach |
| 21 | 1 | 2470.0 | Cap de Creus Natural Park | 60565 | city | full_destination | https://en.wikivoyage.org/wiki/Cap_de_Creus_Natural_Park |
| 22 | 1 | 2470.0 | Perhentian Islands | 56689 | city | full_destination | https://en.wikivoyage.org/wiki/Perhentian_Islands |
| 23 | 1 | 2470.0 | Staten Island | 52800 | city | full_destination | https://en.wikivoyage.org/wiki/Staten_Island |
| 24 | 1 | 2470.0 | Montseny Natural Park | 52049 | city | full_destination | https://en.wikivoyage.org/wiki/Montseny_Natural_Park |
| 25 | 1 | 2470.0 | Galapagos Islands | 48848 | city | full_destination | https://en.wikivoyage.org/wiki/Galapagos_Islands |
| 26 | 1 | 2470.0 | Northern Islands | 46144 | city | full_destination | https://en.wikivoyage.org/wiki/Northern_Islands |
| 27 | 1 | 2470.0 | Angkor Archaeological Park | 41843 | city | full_destination | https://en.wikivoyage.org/wiki/Angkor_Archaeological_Park |
| 28 | 1 | 2470.0 | Gili Islands | 41422 | city | full_destination | https://en.wikivoyage.org/wiki/Gili_Islands |
| 29 | 1 | 2470.0 | Thousand Islands | 41024 | city | full_destination | https://en.wikivoyage.org/wiki/Thousand_Islands |
| 30 | 1 | 2440.0 | Tombstone Territorial Park | 76696 | city | full_destination | https://en.wikivoyage.org/wiki/Tombstone_Territorial_Park |
| 31 | 1 | 2440.0 | Warwick (Rhode Island) | 49331 | city | full_destination | https://en.wikivoyage.org/wiki/Warwick_(Rhode_Island) |
| 32 | 1 | 2440.0 | Kangaroo Island | 40432 | city | full_destination | https://en.wikivoyage.org/wiki/Kangaroo_Island |
| 33 | 1 | 2440.0 | Havelock Island | 39460 | city | full_destination | https://en.wikivoyage.org/wiki/Havelock_Island |
| 34 | 1 | 2440.0 | Menlo Park | 37473 | city | full_destination | https://en.wikivoyage.org/wiki/Menlo_Park |
| 35 | 1 | 2440.0 | Pensacola Beach | 37048 | city | full_destination | https://en.wikivoyage.org/wiki/Pensacola_Beach |
| 36 | 1 | 2440.0 | Virginia Beach | 36365 | city | full_destination | https://en.wikivoyage.org/wiki/Virginia_Beach |
| 37 | 1 | 2440.0 | Ebey's Landing National Historical Reserve | 36280 | city | full_destination | https://en.wikivoyage.org/wiki/Ebey's_Landing_National_Historical_Reserve |
| 38 | 1 | 2440.0 | Block Island | 35169 | city | full_destination | https://en.wikivoyage.org/wiki/Block_Island |
| 39 | 1 | 2440.0 | Wickford (Rhode Island) | 34275 | city | full_destination | https://en.wikivoyage.org/wiki/Wickford_(Rhode_Island) |
| 40 | 1 | 2440.0 | Rehoboth Beach | 34154 | city | full_destination | https://en.wikivoyage.org/wiki/Rehoboth_Beach |
| 41 | 1 | 2440.0 | Saint Helena (island) | 33989 | city | full_destination | https://en.wikivoyage.org/wiki/Saint_Helena_(island) |
| 42 | 1 | 2440.0 | Sant Llorenç del Munt i l'Obac Natural Park | 33199 | city | full_destination | https://en.wikivoyage.org/wiki/Sant_Llorenç_del_Munt_i_l'Obac_Natural_Park |
| 43 | 1 | 2440.0 | Roskilde | 31974 | city | full_destination | https://en.wikivoyage.org/wiki/Roskilde |
| 44 | 1 | 2440.0 | Chatham Islands | 31807 | city | full_destination | https://en.wikivoyage.org/wiki/Chatham_Islands |
| 45 | 1 | 2440.0 | Stewart Island | 30618 | city | full_destination | https://en.wikivoyage.org/wiki/Stewart_Island |
| 46 | 1 | 2440.0 | Laguna Beach | 30601 | city | full_destination | https://en.wikivoyage.org/wiki/Laguna_Beach |
| 47 | 1 | 2440.0 | Shetland Islands | 29919 | city | full_destination | https://en.wikivoyage.org/wiki/Shetland_Islands |
| 48 | 1 | 2440.0 | Collserola Natural Park | 29873 | city | full_destination | https://en.wikivoyage.org/wiki/Collserola_Natural_Park |
| 49 | 1 | 2440.0 | Eskişehir | 29747 | city | full_destination | https://en.wikivoyage.org/wiki/Eskişehir |
| 50 | 1 | 2440.0 | Bruny Island | 29625 | city | full_destination | https://en.wikivoyage.org/wiki/Bruny_Island |
| 51 | 1 | 2440.0 | Great Barrier Island | 29530 | city | full_destination | https://en.wikivoyage.org/wiki/Great_Barrier_Island |
| 52 | 1 | 2440.0 | Bainbridge Island | 28462 | city | full_destination | https://en.wikivoyage.org/wiki/Bainbridge_Island |
| 53 | 1 | 2440.0 | Andaman and Nicobar Islands | 28378 | city | full_destination | https://en.wikivoyage.org/wiki/Andaman_and_Nicobar_Islands |
| 54 | 1 | 2440.0 | Myrtle Beach | 28139 | city | full_destination | https://en.wikivoyage.org/wiki/Myrtle_Beach |
| 55 | 1 | 2440.0 | Jamestown (Rhode Island) | 28002 | city | full_destination | https://en.wikivoyage.org/wiki/Jamestown_(Rhode_Island) |
| 56 | 1 | 2440.0 | West Palm Beach | 27941 | city | full_destination | https://en.wikivoyage.org/wiki/West_Palm_Beach |
| 57 | 1 | 2440.0 | Galiano Island | 27880 | city | full_destination | https://en.wikivoyage.org/wiki/Galiano_Island |
| 58 | 1 | 2440.0 | Cahokia Mounds State Historic Site | 27431 | city | full_destination | https://en.wikivoyage.org/wiki/Cahokia_Mounds_State_Historic_Site |
| 59 | 1 | 2440.0 | Bethany Beach | 27173 | city | full_destination | https://en.wikivoyage.org/wiki/Bethany_Beach |
| 60 | 1 | 2440.0 | Newmarket (Ontario) | 27170 | city | full_destination | https://en.wikivoyage.org/wiki/Newmarket_(Ontario) |
| 61 | 1 | 2440.0 | Nature Park Bulgarka | 26697 | city | full_destination | https://en.wikivoyage.org/wiki/Nature_Park_Bulgarka |
| 62 | 1 | 2440.0 | Gold Beach | 26594 | city | full_destination | https://en.wikivoyage.org/wiki/Gold_Beach |
| 63 | 1 | 2440.0 | Whitsunday Islands | 25612 | city | full_destination | https://en.wikivoyage.org/wiki/Whitsunday_Islands |
| 64 | 1 | 2440.0 | Dewey Beach | 25451 | city | full_destination | https://en.wikivoyage.org/wiki/Dewey_Beach |
| 65 | 1 | 2440.0 | Newport Beach | 25387 | city | full_destination | https://en.wikivoyage.org/wiki/Newport_Beach |
| 66 | 1 | 2440.0 | Albufera Natural Park | 25235 | city | full_destination | https://en.wikivoyage.org/wiki/Albufera_Natural_Park |
| 67 | 1 | 2440.0 | Westhampton Beach | 24527 | city | full_destination | https://en.wikivoyage.org/wiki/Westhampton_Beach |
| 68 | 1 | 2440.0 | Takoma Park | 24518 | city | full_destination | https://en.wikivoyage.org/wiki/Takoma_Park |
| 69 | 1 | 2440.0 | Enniskillen | 24083 | city | full_destination | https://en.wikivoyage.org/wiki/Enniskillen |
| 70 | 1 | 2440.0 | Donghai Island | 24071 | city | full_destination | https://en.wikivoyage.org/wiki/Donghai_Island |
| 71 | 1 | 2440.0 | Savukoski | 23999 | city | full_destination | https://en.wikivoyage.org/wiki/Savukoski |
| 72 | 1 | 2440.0 | Daytona Beach Shores | 23987 | city | full_destination | https://en.wikivoyage.org/wiki/Daytona_Beach_Shores |
| 73 | 1 | 2440.0 | Montserrat Natural Park | 23944 | city | full_destination | https://en.wikivoyage.org/wiki/Montserrat_Natural_Park |
| 74 | 1 | 2440.0 | Natuna Islands | 23880 | city | full_destination | https://en.wikivoyage.org/wiki/Natuna_Islands |
| 75 | 1 | 2440.0 | Waiheke Island | 23692 | city | full_destination | https://en.wikivoyage.org/wiki/Waiheke_Island |
| 76 | 1 | 2440.0 | Morvan Regional Natural Park | 23570 | city | full_destination | https://en.wikivoyage.org/wiki/Morvan_Regional_Natural_Park |
| 77 | 1 | 2440.0 | Estes Park | 23217 | city | full_destination | https://en.wikivoyage.org/wiki/Estes_Park |
| 78 | 1 | 2440.0 | Saint Martins Island | 23170 | city | full_destination | https://en.wikivoyage.org/wiki/Saint_Martins_Island |
| 79 | 1 | 2440.0 | Livingston Island | 23160 | city | full_destination | https://en.wikivoyage.org/wiki/Livingston_Island |
| 80 | 1 | 2440.0 | Salt Spring Island | 22931 | city | full_destination | https://en.wikivoyage.org/wiki/Salt_Spring_Island |
| 81 | 1 | 2440.0 | Bristol (Rhode Island) | 22758 | city | full_destination | https://en.wikivoyage.org/wiki/Bristol_(Rhode_Island) |
| 82 | 1 | 2440.0 | Ormond Beach | 22277 | city | full_destination | https://en.wikivoyage.org/wiki/Ormond_Beach |
| 83 | 1 | 2439.9 | Paldiski | 21955 | city | full_destination | https://en.wikivoyage.org/wiki/Paldiski |
| 84 | 1 | 2439.8 | Orkney Islands | 21868 | city | full_destination | https://en.wikivoyage.org/wiki/Orkney_Islands |
| 85 | 1 | 2439.8 | Panama City Beach | 21864 | city | full_destination | https://en.wikivoyage.org/wiki/Panama_City_Beach |
| 86 | 1 | 2439.8 | Kelleys Island | 21862 | city | full_destination | https://en.wikivoyage.org/wiki/Kelleys_Island |
| 87 | 1 | 2439.6 | Dong Van Karst Plateau Geopark | 21683 | city | full_destination | https://en.wikivoyage.org/wiki/Dong_Van_Karst_Plateau_Geopark |
| 88 | 1 | 2439.4 | Park City (Utah) | 21566 | city | full_destination | https://en.wikivoyage.org/wiki/Park_City_(Utah) |
| 89 | 1 | 2439.0 | Rimouski | 21228 | city | full_destination | https://en.wikivoyage.org/wiki/Rimouski |
| 90 | 1 | 2438.9 | Little Corn Island | 21161 | city | full_destination | https://en.wikivoyage.org/wiki/Little_Corn_Island |
| 91 | 1 | 2438.8 | Long Beach (New York) | 21134 | city | full_destination | https://en.wikivoyage.org/wiki/Long_Beach_(New_York) |
| 92 | 1 | 2438.7 | Skipton | 20989 | city | full_destination | https://en.wikivoyage.org/wiki/Skipton |
| 93 | 1 | 2438.6 | Sado Island | 20938 | city | full_destination | https://en.wikivoyage.org/wiki/Sado_Island |
| 94 | 1 | 2438.0 | South Padre Island | 20518 | city | full_destination | https://en.wikivoyage.org/wiki/South_Padre_Island |
| 95 | 1 | 2438.0 | Phillip Island | 20514 | city | full_destination | https://en.wikivoyage.org/wiki/Phillip_Island |
| 96 | 1 | 2437.7 | College Park (Maryland) | 20296 | city | full_destination | https://en.wikivoyage.org/wiki/College_Park_(Maryland) |
| 97 | 1 | 2437.7 | Margarita Island | 20258 | city | full_destination | https://en.wikivoyage.org/wiki/Margarita_Island |
| 98 | 1 | 2437.5 | Saguenay-Saint Lawrence Marine Park | 20115 | city | full_destination | https://en.wikivoyage.org/wiki/Saguenay-Saint_Lawrence_Marine_Park |
| 99 | 1 | 2437.4 | Cape Breton Island | 20083 | city | full_destination | https://en.wikivoyage.org/wiki/Cape_Breton_Island |
| 100 | 1 | 2437.3 | Cocoa Beach | 20035 | city | full_destination | https://en.wikivoyage.org/wiki/Cocoa_Beach |
