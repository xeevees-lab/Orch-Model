import 'dart:convert';
import 'package:http/http.dart' as http;

/// Talks only to the visitor-facing service. Nothing else is reachable
/// from this app, by design.
///
/// A phone cannot see the machine's localhost. Set this to your
/// computer's address on the same Wi-Fi, for example
/// http://192.168.1.14:8400 — find it with `ipconfig` on Windows or
/// `hostname -I` in WSL.
const String kApiBase = String.fromEnvironment(
  'API_BASE',
  defaultValue: 'http://10.0.2.2:8400',
);

class ApiException implements Exception {
  final String message;
  ApiException(this.message);
  @override
  String toString() => message;
}

class Api {
  static final _client = http.Client();

  static Future<dynamic> _get(String path) async {
    try {
      final r = await _client
          .get(Uri.parse('$kApiBase$path'))
          .timeout(const Duration(seconds: 8));
      if (r.statusCode != 200) {
        throw ApiException('Could not load (${r.statusCode})');
      }
      return jsonDecode(r.body);
    } catch (e) {
      if (e is ApiException) rethrow;
      throw ApiException('No connection');
    }
  }

  static Future<dynamic> _post(String path, [Map<String, dynamic>? body]) async {
    try {
      final r = await _client
          .post(
            Uri.parse('$kApiBase$path'),
            headers: {'Content-Type': 'application/json'},
            body: body == null ? null : jsonEncode(body),
          )
          .timeout(const Duration(seconds: 8));
      if (r.statusCode != 200) {
        throw ApiException('Could not load (${r.statusCode})');
      }
      return jsonDecode(r.body);
    } catch (e) {
      if (e is ApiException) rethrow;
      throw ApiException('No connection');
    }
  }

  static Future<NowInfo> now() async => NowInfo.from(await _get('/now'));

  static Future<List<Venue>> venues({String sort = 'quietest'}) async {
    final list = await _get('/venues?sort=$sort') as List;
    return list.map((e) => Venue.from(e)).toList();
  }

  static Future<Venue> venue(int id) async =>
      Venue.from(await _get('/venues/$id'));

  static Future<List<Venue>> alternatives(int id) async {
    final list = await _get('/alternatives/$id') as List;
    return list.map((e) => Venue.from(e)).toList();
  }

  static Future<List<Stay>> stays({
    int partySize = 2,
    String budget = 'mid',
    bool stepFree = false,
  }) async {
    final list = await _post('/stays', {
      'party_size': partySize,
      'budget': budget,
      'needs_step_free': stepFree,
    }) as List;
    return list.map((e) => Stay.from(e)).toList();
  }

  static Future<Journey> journey(String fromZone, int toVenue) async =>
      Journey.from(await _get('/journey?from_zone=$fromZone&to_venue=$toVenue'));

  static Future<List<Offer>> offers() async {
    final list = await _get('/offers') as List;
    return list.map((e) => Offer.from(e)).toList();
  }

  static Future<List<HelpPoint>> help() async {
    final list = await _get('/help') as List;
    return list.map((e) => HelpPoint.from(e)).toList();
  }

  static Future<void> report(int venueId, String level) =>
      _post('/report?venue_id=$venueId&level=$level');
}

// ---------------------------------------------------------------- models

class NowInfo {
  final String? clock;
  final int? festivalDay;
  final String headline;
  final String advisory;
  NowInfo(this.clock, this.festivalDay, this.headline, this.advisory);
  factory NowInfo.from(Map<String, dynamic> j) => NowInfo(
        j['clock'], j['festival_day'],
        j['headline'] ?? '', j['advisory'] ?? '',
      );
}

class Venue {
  final int id;
  final String name;
  final String kind;
  final String crowd;
  final String note;
  final String wait;
  final int waitMinutes;
  final double lat;
  final double lon;
  final String? openFrom;
  final String? openUntil;
  final String? bestTime;

  Venue({
    required this.id, required this.name, required this.kind,
    required this.crowd, required this.note, required this.wait,
    required this.waitMinutes, required this.lat, required this.lon,
    this.openFrom, this.openUntil, this.bestTime,
  });

  factory Venue.from(Map<String, dynamic> j) => Venue(
        id: j['id'],
        name: j['name'] ?? '',
        kind: j['kind'] ?? '',
        crowd: j['crowd'] ?? 'clear',
        note: j['note'] ?? '',
        wait: j['wait'] ?? '',
        waitMinutes: j['wait_minutes'] ?? 0,
        lat: (j['lat'] ?? 0).toDouble(),
        lon: (j['lon'] ?? 0).toDouble(),
        openFrom: j['open_from'],
        openUntil: j['open_until'],
        bestTime: j['best_time'],
      );
}

class Stay {
  final String zoneId;
  final String area;
  final int pricePerNight;
  final int roomsAvailable;
  final String travel;
  final int travelMinutes;
  final String why;
  final bool fitsBudget;

  Stay({
    required this.zoneId, required this.area, required this.pricePerNight,
    required this.roomsAvailable, required this.travel,
    required this.travelMinutes, required this.why, required this.fitsBudget,
  });

  factory Stay.from(Map<String, dynamic> j) => Stay(
        zoneId: j['zone_id'],
        area: j['area'] ?? '',
        pricePerNight: j['price_per_night'] ?? 0,
        roomsAvailable: j['rooms_available'] ?? 0,
        travel: j['travel'] ?? '',
        travelMinutes: j['travel_minutes'] ?? 0,
        why: j['why'] ?? '',
        fitsBudget: j['fits_budget'] ?? true,
      );
}

class JourneyStep {
  final String mode;
  final String detail;
  final int minutes;
  JourneyStep(this.mode, this.detail, this.minutes);
  factory JourneyStep.from(Map<String, dynamic> j) =>
      JourneyStep(j['mode'], j['detail'], j['minutes']);
}

class Journey {
  final String destination;
  final String crowd;
  final String wait;
  final List<JourneyStep> steps;
  final String total;
  final String leaveBy;
  final String tip;

  Journey({
    required this.destination, required this.crowd, required this.wait,
    required this.steps, required this.total, required this.leaveBy,
    required this.tip,
  });

  factory Journey.from(Map<String, dynamic> j) => Journey(
        destination: j['destination'] ?? '',
        crowd: j['crowd'] ?? 'clear',
        wait: j['wait'] ?? '',
        steps: (j['steps'] as List).map((e) => JourneyStep.from(e)).toList(),
        total: j['total'] ?? '',
        leaveBy: j['leave_by'] ?? '',
        tip: j['tip'] ?? '',
      );
}

class Offer {
  final String id;
  final String title;
  final String detail;
  final int expiresInMin;
  final String kind;
  Offer(this.id, this.title, this.detail, this.expiresInMin, this.kind);
  factory Offer.from(Map<String, dynamic> j) => Offer(
        j['id'], j['title'] ?? '', j['detail'] ?? '',
        j['expires_in_min'] ?? 0, j['kind'] ?? 'venue',
      );
}

class HelpPoint {
  final int id;
  final String name;
  final String type;
  final double lat;
  final double lon;
  HelpPoint(this.id, this.name, this.type, this.lat, this.lon);
  factory HelpPoint.from(Map<String, dynamic> j) => HelpPoint(
        j['id'], j['name'] ?? '', j['type'] ?? '',
        (j['lat'] ?? 0).toDouble(), (j['lon'] ?? 0).toDouble(),
      );
}
