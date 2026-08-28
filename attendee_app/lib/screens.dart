import 'package:flutter/material.dart';
import 'api.dart';
import 'theme.dart';

// ================================================================ shared

class CrowdDot extends StatelessWidget {
  final String level;
  final double size;
  const CrowdDot(this.level, {super.key, this.size = 9});

  @override
  Widget build(BuildContext context) => Container(
        width: size,
        height: size,
        decoration: BoxDecoration(
          color: Palette.crowd(level),
          shape: BoxShape.circle,
        ),
      );
}

class SectionTitle extends StatelessWidget {
  final String text;
  final String? action;
  final VoidCallback? onAction;
  const SectionTitle(this.text, {super.key, this.action, this.onAction});

  @override
  Widget build(BuildContext context) => Padding(
        padding: const EdgeInsets.fromLTRB(4, 22, 4, 10),
        child: Row(
          mainAxisAlignment: MainAxisAlignment.spaceBetween,
          children: [
            Text(text, style: Theme.of(context).textTheme.titleMedium),
            if (action != null)
              GestureDetector(
                onTap: onAction,
                child: Text(action!,
                    style: Theme.of(context)
                        .textTheme
                        .bodySmall
                        ?.copyWith(color: Palette.plum,
                            fontWeight: FontWeight.w600)),
              ),
          ],
        ),
      );
}

class Loading extends StatelessWidget {
  const Loading({super.key});
  @override
  Widget build(BuildContext context) => const Padding(
        padding: EdgeInsets.symmetric(vertical: 56),
        child: Center(
          child: SizedBox(
            width: 22, height: 22,
            child: CircularProgressIndicator(strokeWidth: 2.2),
          ),
        ),
      );
}

class Problem extends StatelessWidget {
  final String message;
  final VoidCallback onRetry;
  const Problem(this.message, this.onRetry, {super.key});

  @override
  Widget build(BuildContext context) => Padding(
        padding: const EdgeInsets.symmetric(vertical: 48, horizontal: 24),
        child: Column(
          children: [
            const Icon(Icons.cloud_off_rounded, color: Palette.muted, size: 30),
            const SizedBox(height: 12),
            Text(message,
                textAlign: TextAlign.center,
                style: Theme.of(context).textTheme.bodyMedium),
            const SizedBox(height: 16),
            OutlinedButton(onPressed: onRetry, child: const Text('Try again')),
          ],
        ),
      );
}

/// Loads once, rebuilds on pull-to-refresh. Keeps every screen's
/// loading, error and empty handling identical.
class Loader<T> extends StatefulWidget {
  final Future<T> Function() load;
  final Widget Function(T data) builder;
  const Loader({super.key, required this.load, required this.builder});

  @override
  State<Loader<T>> createState() => _LoaderState<T>();
}

class _LoaderState<T> extends State<Loader<T>> {
  late Future<T> _future;

  @override
  void initState() {
    super.initState();
    _future = _attach(widget.load());
  }

  /// Attach a no-op listener so a failure is never "unhandled" before
  /// FutureBuilder can render it. The arrow form of setState also
  /// returned the Future itself, which Flutter warns about.
  Future<T> _attach(Future<T> f) {
    f.then((_) {}, onError: (_) {});
    return f;
  }

  void _refresh() {
    final f = _attach(widget.load());
    setState(() {
      _future = f;
    });
  }

  @override
  Widget build(BuildContext context) => FutureBuilder<T>(
        future: _future,
        builder: (context, snap) {
          if (snap.connectionState == ConnectionState.waiting) {
            return const Loading();
          }
          if (snap.hasError) {
            return Problem('${snap.error}', _refresh);
          }
          return RefreshIndicator(
            color: Palette.plum,
            onRefresh: () async => _refresh(),
            child: widget.builder(snap.data as T),
          );
        },
      );
}

// ================================================================ today

class TodayScreen extends StatelessWidget {
  final void Function(int venueId) onOpenVenue;
  const TodayScreen({super.key, required this.onOpenVenue});

  @override
  Widget build(BuildContext context) {
    return Loader<(NowInfo, List<Venue>)>(
      load: () async => (await Api.now(), await Api.venues()),
      builder: (data) {
        final (now, venues) = data;
        final quiet = venues.take(3).toList();
        final packed =
            venues.where((v) => v.crowd == 'packed').take(2).toList();

        return ListView(
          padding: const EdgeInsets.fromLTRB(16, 4, 16, 32),
          children: [
            _Headline(now),
            const SectionTitle('Quietest right now'),
            ...quiet.map((v) => Padding(
                  padding: const EdgeInsets.only(bottom: 10),
                  child: _VenueCard(v, onTap: () => onOpenVenue(v.id)),
                )),
            if (packed.isNotEmpty) ...[
              const SectionTitle('Worth avoiding for now'),
              ...packed.map((v) => Padding(
                    padding: const EdgeInsets.only(bottom: 10),
                    child: _VenueCard(v, onTap: () => onOpenVenue(v.id)),
                  )),
            ],
            const SizedBox(height: 20),
            _AdviceCard(now.advisory),
          ],
        );
      },
    );
  }
}

class _Headline extends StatelessWidget {
  final NowInfo now;
  const _Headline(this.now);

  @override
  Widget build(BuildContext context) {
    final t = Theme.of(context).textTheme;
    return Container(
      width: double.infinity,
      padding: const EdgeInsets.fromLTRB(20, 22, 20, 22),
      decoration: BoxDecoration(
        gradient: const LinearGradient(
          begin: Alignment.topLeft,
          end: Alignment.bottomRight,
          colors: [Palette.plum, Palette.plumDark],
        ),
        borderRadius: BorderRadius.circular(20),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              const Icon(Icons.auto_awesome_rounded,
                  size: 15, color: Palette.saffron),
              const SizedBox(width: 7),
              Text(
                now.festivalDay != null
                    ? 'DAY ${now.festivalDay} OF THE FESTIVAL'
                    : 'FESTIVAL COMPANION',
                style: t.labelSmall?.copyWith(
                    color: Palette.saffron, letterSpacing: 1.1),
              ),
            ],
          ),
          const SizedBox(height: 12),
          Text(now.headline,
              style: t.headlineSmall?.copyWith(
                  color: Colors.white, height: 1.35, fontSize: 19)),
        ],
      ),
    );
  }
}

class _AdviceCard extends StatelessWidget {
  final String text;
  const _AdviceCard(this.text);

  @override
  Widget build(BuildContext context) => Container(
        padding: const EdgeInsets.all(16),
        decoration: BoxDecoration(
          color: Palette.sand,
          borderRadius: BorderRadius.circular(16),
        ),
        child: Row(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            const Icon(Icons.lightbulb_outline_rounded,
                size: 19, color: Palette.saffron),
            const SizedBox(width: 12),
            Expanded(
              child: Text(text,
                  style: Theme.of(context).textTheme.bodyMedium),
            ),
          ],
        ),
      );
}

class _VenueCard extends StatelessWidget {
  final Venue v;
  final VoidCallback onTap;
  const _VenueCard(this.v, {required this.onTap});

  @override
  Widget build(BuildContext context) {
    final t = Theme.of(context).textTheme;
    return Card(
      child: InkWell(
        onTap: onTap,
        borderRadius: BorderRadius.circular(18),
        child: Padding(
          padding: const EdgeInsets.fromLTRB(16, 14, 14, 14),
          child: Row(
            children: [
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(v.name, style: t.titleSmall),
                    const SizedBox(height: 5),
                    Row(
                      children: [
                        CrowdDot(v.crowd),
                        const SizedBox(width: 6),
                        Text(Palette.crowdLabel(v.crowd),
                            style: t.bodySmall?.copyWith(
                                color: Palette.crowd(v.crowd),
                                fontWeight: FontWeight.w600)),
                        Text('  ·  ${v.wait}', style: t.bodySmall),
                      ],
                    ),
                  ],
                ),
              ),
              const Icon(Icons.chevron_right_rounded,
                  color: Palette.muted, size: 22),
            ],
          ),
        ),
      ),
    );
  }
}

// ================================================================ places

class PlacesScreen extends StatefulWidget {
  final void Function(int venueId) onOpenVenue;
  const PlacesScreen({super.key, required this.onOpenVenue});

  @override
  State<PlacesScreen> createState() => _PlacesScreenState();
}

class _PlacesScreenState extends State<PlacesScreen> {
  String _filter = 'all';

  @override
  Widget build(BuildContext context) {
    return Loader<List<Venue>>(
      load: Api.venues,
      builder: (venues) {
        final shown = _filter == 'all'
            ? venues
            : venues.where((v) => v.kind == _filter).toList();
        return ListView(
          padding: const EdgeInsets.fromLTRB(16, 8, 16, 32),
          children: [
            Wrap(
              spacing: 8,
              children: [
                _chip('all', 'Everywhere'),
                _chip('darshan', 'Darshan'),
                _chip('immersion', 'Immersion'),
              ],
            ),
            const SizedBox(height: 18),
            ...shown.map((v) => Padding(
                  padding: const EdgeInsets.only(bottom: 10),
                  child: _VenueCard(v,
                      onTap: () => widget.onOpenVenue(v.id)),
                )),
          ],
        );
      },
    );
  }

  Widget _chip(String key, String label) {
    final on = _filter == key;
    return GestureDetector(
      onTap: () => setState(() => _filter = key),
      child: Container(
        padding: const EdgeInsets.symmetric(horizontal: 15, vertical: 9),
        decoration: BoxDecoration(
          color: on ? Palette.plum : Palette.sand,
          borderRadius: BorderRadius.circular(11),
        ),
        child: Text(label,
            style: Theme.of(context).textTheme.bodySmall?.copyWith(
                  color: on ? Colors.white : Palette.ink,
                  fontWeight: FontWeight.w600,
                )),
      ),
    );
  }
}

// ================================================================ stay

class StayScreen extends StatefulWidget {
  const StayScreen({super.key});
  @override
  State<StayScreen> createState() => _StayScreenState();
}

class _StayScreenState extends State<StayScreen> {
  String _budget = 'mid';
  int _party = 2;
  final int _reload = 0;

  @override
  Widget build(BuildContext context) {
    final t = Theme.of(context).textTheme;
    return ListView(
      padding: const EdgeInsets.fromLTRB(16, 8, 16, 32),
      children: [
        Text('What suits you?', style: t.titleMedium),
        const SizedBox(height: 12),
        Row(
          children: [
            Expanded(child: _budgetPicker()),
            const SizedBox(width: 10),
            _partyPicker(),
          ],
        ),
        const SizedBox(height: 6),
        SizedBox(
          height: 620,
          child: Loader<List<Stay>>(
            key: ValueKey('$_budget-$_party-$_reload'),
            load: () => Api.stays(partySize: _party, budget: _budget),
            builder: (stays) {
              if (stays.isEmpty) {
                return Padding(
                  padding: const EdgeInsets.symmetric(vertical: 48),
                  child: Text('Nothing available with those filters.',
                      textAlign: TextAlign.center, style: t.bodySmall),
                );
              }
              return ListView(
                padding: const EdgeInsets.only(top: 14),
                children: [
                  ...stays.asMap().entries.map((e) => Padding(
                        padding: const EdgeInsets.only(bottom: 10),
                        child: _StayCard(e.value, best: e.key == 0),
                      )),
                ],
              );
            },
          ),
        ),
      ],
    );
  }

  Widget _budgetPicker() => SegmentedButton<String>(
        segments: const [
          ButtonSegment(value: 'low', label: Text('₹')),
          ButtonSegment(value: 'mid', label: Text('₹₹')),
          ButtonSegment(value: 'high', label: Text('₹₹₹')),
        ],
        selected: {_budget},
        showSelectedIcon: false,
        onSelectionChanged: (s) => setState(() => _budget = s.first),
        style: ButtonStyle(
          backgroundColor: WidgetStateProperty.resolveWith((states) =>
              states.contains(WidgetState.selected)
                  ? Palette.plum
                  : Palette.sand),
          foregroundColor: WidgetStateProperty.resolveWith((states) =>
              states.contains(WidgetState.selected)
                  ? Colors.white
                  : Palette.ink),
          side: const WidgetStatePropertyAll(BorderSide.none),
        ),
      );

  Widget _partyPicker() => Container(
        padding: const EdgeInsets.symmetric(horizontal: 6),
        decoration: BoxDecoration(
          color: Palette.sand,
          borderRadius: BorderRadius.circular(20),
        ),
        child: Row(
          children: [
            IconButton(
              icon: const Icon(Icons.remove_rounded, size: 18),
              onPressed: _party > 1 ? () => setState(() => _party--) : null,
            ),
            Text('$_party',
                style: Theme.of(context).textTheme.titleSmall),
            IconButton(
              icon: const Icon(Icons.add_rounded, size: 18),
              onPressed: _party < 12 ? () => setState(() => _party++) : null,
            ),
          ],
        ),
      );
}

class _StayCard extends StatelessWidget {
  final Stay s;
  final bool best;
  const _StayCard(this.s, {this.best = false});

  @override
  Widget build(BuildContext context) {
    final t = Theme.of(context).textTheme;
    return Card(
      child: Padding(
        padding: const EdgeInsets.fromLTRB(16, 14, 16, 14),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                Expanded(child: Text(s.area, style: t.titleSmall)),
                if (best)
                  Container(
                    padding:
                        const EdgeInsets.symmetric(horizontal: 9, vertical: 3),
                    decoration: BoxDecoration(
                      color: Palette.saffron.withValues(alpha: 0.16),
                      borderRadius: BorderRadius.circular(8),
                    ),
                    child: Text('BEST MATCH',
                        style: t.labelSmall
                            ?.copyWith(color: const Color(0xFF9A5B10))),
                  ),
              ],
            ),
            const SizedBox(height: 8),
            Row(
              children: [
                Text('₹${s.pricePerNight}',
                    style: t.titleMedium?.copyWith(fontSize: 18)),
                Text(' / night', style: t.bodySmall),
                const Spacer(),
                const Icon(Icons.schedule_rounded,
                    size: 14, color: Palette.muted),
                const SizedBox(width: 4),
                Text(s.travel, style: t.bodySmall),
              ],
            ),
            const SizedBox(height: 10),
            Text(s.why, style: t.bodySmall),
            const SizedBox(height: 12),
            Row(
              children: [
                const Icon(Icons.meeting_room_outlined,
                    size: 14, color: Palette.muted),
                const SizedBox(width: 5),
                Text('${s.roomsAvailable} rooms free', style: t.bodySmall),
                const Spacer(),
                OutlinedButton(
                  onPressed: () => ScaffoldMessenger.of(context).showSnackBar(
                    SnackBar(
                      content: Text('Saved ${s.area} to your plan'),
                      behavior: SnackBarBehavior.floating,
                    ),
                  ),
                  style: OutlinedButton.styleFrom(
                    minimumSize: const Size(0, 38),
                    padding: const EdgeInsets.symmetric(horizontal: 16),
                  ),
                  child: const Text('Save'),
                ),
              ],
            ),
          ],
        ),
      ),
    );
  }
}

// ================================================================ offers

class OffersScreen extends StatelessWidget {
  const OffersScreen({super.key});

  @override
  Widget build(BuildContext context) {
    final t = Theme.of(context).textTheme;
    return Loader<List<Offer>>(
      load: Api.offers,
      builder: (offers) => ListView(
        padding: const EdgeInsets.fromLTRB(16, 8, 16, 32),
        children: [
          Text('Perks for being flexible',
              style: t.headlineSmall?.copyWith(fontSize: 19)),
          const SizedBox(height: 6),
          Text(
            'Going somewhere quieter, or a little earlier, usually gets you '
            'something back.',
            style: t.bodySmall,
          ),
          const SizedBox(height: 20),
          ...offers.map((o) => Padding(
                padding: const EdgeInsets.only(bottom: 10),
                child: _OfferCard(o),
              )),
        ],
      ),
    );
  }
}

class _OfferCard extends StatelessWidget {
  final Offer o;
  const _OfferCard(this.o);

  @override
  Widget build(BuildContext context) {
    final t = Theme.of(context).textTheme;
    final icon = o.kind == 'travel'
        ? Icons.directions_transit_rounded
        : Icons.local_cafe_rounded;
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Row(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Container(
              width: 42, height: 42,
              decoration: BoxDecoration(
                color: Palette.saffron.withValues(alpha: 0.14),
                borderRadius: BorderRadius.circular(12),
              ),
              child: Icon(icon, size: 20, color: Palette.saffron),
            ),
            const SizedBox(width: 14),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(o.title, style: t.titleSmall),
                  const SizedBox(height: 5),
                  Text(o.detail, style: t.bodySmall),
                  const SizedBox(height: 10),
                  Row(
                    children: [
                      const Icon(Icons.timer_outlined,
                          size: 13, color: Palette.muted),
                      const SizedBox(width: 4),
                      Text('Expires in ${o.expiresInMin} min',
                          style: t.bodySmall),
                      const Spacer(),
                      FilledButton(
                        onPressed: () =>
                            ScaffoldMessenger.of(context).showSnackBar(
                          const SnackBar(
                            content: Text('Added to your wallet'),
                            behavior: SnackBarBehavior.floating,
                          ),
                        ),
                        style: FilledButton.styleFrom(
                          minimumSize: const Size(0, 36),
                          padding: const EdgeInsets.symmetric(horizontal: 18),
                        ),
                        child: const Text('Claim'),
                      ),
                    ],
                  ),
                ],
              ),
            ),
          ],
        ),
      ),
    );
  }
}

// ================================================================ venue

class VenueSheet extends StatelessWidget {
  final int venueId;
  const VenueSheet({super.key, required this.venueId});

  @override
  Widget build(BuildContext context) {
    final t = Theme.of(context).textTheme;
    return DraggableScrollableSheet(
      initialChildSize: 0.72,
      minChildSize: 0.5,
      maxChildSize: 0.94,
      expand: false,
      builder: (context, controller) => Container(
        decoration: const BoxDecoration(
          color: Palette.cream,
          borderRadius: BorderRadius.vertical(top: Radius.circular(24)),
        ),
        child: Loader<(Venue, List<Venue>)>(
          load: () async =>
              (await Api.venue(venueId), await Api.alternatives(venueId)),
          builder: (data) {
            final (v, alts) = data;
            return ListView(
              controller: controller,
              padding: const EdgeInsets.fromLTRB(20, 12, 20, 32),
              children: [
                Center(
                  child: Container(
                    width: 38, height: 4,
                    decoration: BoxDecoration(
                      color: Palette.line,
                      borderRadius: BorderRadius.circular(2),
                    ),
                  ),
                ),
                const SizedBox(height: 20),
                Text(v.name, style: t.headlineSmall),
                const SizedBox(height: 12),
                Container(
                  padding: const EdgeInsets.all(16),
                  decoration: BoxDecoration(
                    color: Palette.crowd(v.crowd).withValues(alpha: 0.10),
                    borderRadius: BorderRadius.circular(14),
                  ),
                  child: Row(
                    children: [
                      CrowdDot(v.crowd, size: 11),
                      const SizedBox(width: 10),
                      Expanded(
                        child: Column(
                          crossAxisAlignment: CrossAxisAlignment.start,
                          children: [
                            Text(Palette.crowdLabel(v.crowd),
                                style: t.titleSmall?.copyWith(
                                    color: Palette.crowd(v.crowd))),
                            Text(v.note, style: t.bodySmall),
                          ],
                        ),
                      ),
                      Text(v.wait, style: t.titleSmall),
                    ],
                  ),
                ),
                if (v.openFrom != null) ...[
                  const SizedBox(height: 16),
                  _row(context, Icons.schedule_rounded,
                      'Open ${v.openFrom} – ${v.openUntil}'),
                ],
                if (v.bestTime != null)
                  _row(context, Icons.trending_down_rounded,
                      'Quietest ${v.bestTime!.toLowerCase()}'),
                if (alts.isNotEmpty) ...[
                  const SectionTitle('Quieter nearby'),
                  ...alts.map((a) => Padding(
                        padding: const EdgeInsets.only(bottom: 8),
                        child: _VenueCard(a, onTap: () {
                          Navigator.pop(context);
                          showModalBottomSheet(
                            context: context,
                            isScrollControlled: true,
                            backgroundColor: Colors.transparent,
                            builder: (_) => VenueSheet(venueId: a.id),
                          );
                        }),
                      )),
                ],
                const SizedBox(height: 20),
                FilledButton.icon(
                  onPressed: () => ScaffoldMessenger.of(context).showSnackBar(
                    const SnackBar(
                      content: Text('Directions coming soon'),
                      behavior: SnackBarBehavior.floating,
                    ),
                  ),
                  icon: const Icon(Icons.navigation_rounded, size: 18),
                  label: const Text('Get directions'),
                ),
                const SizedBox(height: 10),
                OutlinedButton.icon(
                  onPressed: () => _report(context, v.id),
                  icon: const Icon(Icons.campaign_outlined, size: 17),
                  label: const Text('Report how busy it is'),
                ),
              ],
            );
          },
        ),
      ),
    );
  }

  Widget _row(BuildContext context, IconData icon, String text) => Padding(
        padding: const EdgeInsets.symmetric(vertical: 6),
        child: Row(
          children: [
            Icon(icon, size: 16, color: Palette.muted),
            const SizedBox(width: 10),
            Text(text, style: Theme.of(context).textTheme.bodyMedium),
          ],
        ),
      );

  void _report(BuildContext context, int id) {
    showModalBottomSheet(
      context: context,
      backgroundColor: Palette.cream,
      shape: const RoundedRectangleBorder(
        borderRadius: BorderRadius.vertical(top: Radius.circular(22)),
      ),
      builder: (ctx) => Padding(
        padding: const EdgeInsets.fromLTRB(20, 22, 20, 34),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text('How is it where you are?',
                style: Theme.of(ctx).textTheme.titleMedium),
            const SizedBox(height: 16),
            ...[
              ('ok', 'Fine, moving freely', Palette.clear),
              ('busy', 'Crowded but okay', Palette.busy),
              ('unsafe', 'Too crowded, feels unsafe', Palette.packed),
            ].map((o) => Padding(
                  padding: const EdgeInsets.only(bottom: 8),
                  child: OutlinedButton(
                    onPressed: () async {
                      Navigator.pop(ctx);
                      await Api.report(id, o.$1);
                      if (context.mounted) {
                        ScaffoldMessenger.of(context).showSnackBar(
                          const SnackBar(
                            content: Text('Thanks — that helps others'),
                            behavior: SnackBarBehavior.floating,
                          ),
                        );
                      }
                    },
                    style: OutlinedButton.styleFrom(
                      alignment: Alignment.centerLeft,
                      foregroundColor: o.$3,
                    ),
                    child: Row(
                      children: [
                        CrowdDot(o.$1 == 'ok'
                            ? 'clear'
                            : o.$1 == 'busy'
                                ? 'busy'
                                : 'packed'),
                        const SizedBox(width: 12),
                        Text(o.$2),
                      ],
                    ),
                  ),
                )),
          ],
        ),
      ),
    );
  }
}
