import 'package:flutter/material.dart';
import 'theme.dart';
import 'screens.dart';

void main() => runApp(const CompanionApp());

class CompanionApp extends StatelessWidget {
  const CompanionApp({super.key});

  @override
  Widget build(BuildContext context) => MaterialApp(
        title: 'Festival Companion',
        debugShowCheckedModeBanner: false,
        theme: buildTheme(),
        home: const Home(),
      );
}

class Home extends StatefulWidget {
  const Home({super.key});
  @override
  State<Home> createState() => _HomeState();
}

class _HomeState extends State<Home> {
  int _tab = 0;

  static const _titles = ['Today', 'Places', 'Stay', 'Perks'];

  void _openVenue(int id) => showModalBottomSheet(
        context: context,
        isScrollControlled: true,
        backgroundColor: Colors.transparent,
        builder: (_) => VenueSheet(venueId: id),
      );

  @override
  Widget build(BuildContext context) {
    final pages = [
      TodayScreen(onOpenVenue: _openVenue),
      PlacesScreen(onOpenVenue: _openVenue),
      const StayScreen(),
      const OffersScreen(),
    ];

    return Scaffold(
      appBar: AppBar(
        title: Text(_titles[_tab]),
        actions: [
          IconButton(
            icon: const Icon(Icons.help_outline_rounded),
            tooltip: 'Help and safety',
            onPressed: _showHelp,
          ),
          const SizedBox(width: 4),
        ],
      ),
      body: SafeArea(child: pages[_tab]),
      bottomNavigationBar: NavigationBar(
        selectedIndex: _tab,
        onDestinationSelected: (i) => setState(() => _tab = i),
        destinations: const [
          NavigationDestination(
            icon: Icon(Icons.today_outlined),
            selectedIcon: Icon(Icons.today_rounded),
            label: 'Today',
          ),
          NavigationDestination(
            icon: Icon(Icons.place_outlined),
            selectedIcon: Icon(Icons.place_rounded),
            label: 'Places',
          ),
          NavigationDestination(
            icon: Icon(Icons.hotel_outlined),
            selectedIcon: Icon(Icons.hotel_rounded),
            label: 'Stay',
          ),
          NavigationDestination(
            icon: Icon(Icons.redeem_outlined),
            selectedIcon: Icon(Icons.redeem_rounded),
            label: 'Perks',
          ),
        ],
      ),
    );
  }

  void _showHelp() => showModalBottomSheet(
        context: context,
        backgroundColor: Palette.cream,
        shape: const RoundedRectangleBorder(
          borderRadius: BorderRadius.vertical(top: Radius.circular(22)),
        ),
        builder: (ctx) {
          final t = Theme.of(ctx).textTheme;
          return Padding(
            padding: const EdgeInsets.fromLTRB(20, 22, 20, 34),
            child: Column(
              mainAxisSize: MainAxisSize.min,
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text('Help and safety', style: t.titleMedium),
                const SizedBox(height: 14),
                _tile(ctx, Icons.local_hospital_rounded, 'Medical help',
                    'Find the nearest medical point'),
                _tile(ctx, Icons.water_drop_rounded, 'Drinking water',
                    'Water points near you'),
                _tile(ctx, Icons.groups_rounded, 'Lost someone',
                    'What to do if you get separated'),
                const SizedBox(height: 14),
                FilledButton.icon(
                  onPressed: () {
                    Navigator.pop(ctx);
                    ScaffoldMessenger.of(context).showSnackBar(
                      const SnackBar(
                        content: Text('Emergency services: 112'),
                        behavior: SnackBarBehavior.floating,
                      ),
                    );
                  },
                  style: FilledButton.styleFrom(
                    backgroundColor: Palette.packed,
                  ),
                  icon: const Icon(Icons.emergency_rounded, size: 18),
                  label: const Text('Emergency'),
                ),
              ],
            ),
          );
        },
      );

  Widget _tile(BuildContext ctx, IconData icon, String title, String sub) =>
      Padding(
        padding: const EdgeInsets.symmetric(vertical: 7),
        child: Row(
          children: [
            Container(
              width: 38, height: 38,
              decoration: BoxDecoration(
                color: Palette.sand,
                borderRadius: BorderRadius.circular(11),
              ),
              child: Icon(icon, size: 18, color: Palette.plum),
            ),
            const SizedBox(width: 13),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(title, style: Theme.of(ctx).textTheme.titleSmall),
                  Text(sub, style: Theme.of(ctx).textTheme.bodySmall),
                ],
              ),
            ),
          ],
        ),
      );
}
