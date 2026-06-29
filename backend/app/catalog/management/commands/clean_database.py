from django.core.management.base import BaseCommand
from catalog.models import (
    Item,
    Summary,
    NetworkNode,
    NetworkEdge,
    Department,
    DataPerson,
    SourceRunLog,
    DestinationRunLog,
    ChatSession,
)

class Command(BaseCommand):
    help = 'Cleans all data from the database (Item, Summary, NetworkNode, NetworkEdge, Department, DataPerson, SourceRunLog, DestinationRunLog, ChatSession)'

    def handle(self, *args, **options):
        self.stdout.write("Starting database cleanup...")

        deleted_items, _ = Item.objects.all().delete()
        self.stdout.write(f"Deleted {deleted_items} Items.")

        deleted_summaries, _ = Summary.objects.all().delete()
        self.stdout.write(f"Deleted {deleted_summaries} Summaries.")

        deleted_nodes, _ = NetworkNode.objects.all().delete()
        self.stdout.write(f"Deleted {deleted_nodes} NetworkNodes.")

        deleted_edges, _ = NetworkEdge.objects.all().delete()
        self.stdout.write(f"Deleted {deleted_edges} NetworkEdges.")

        deleted_departments, _ = Department.objects.all().delete()
        self.stdout.write(f"Deleted {deleted_departments} Departments.")

        deleted_persons, _ = DataPerson.objects.all().delete()
        self.stdout.write(f"Deleted {deleted_persons} DataPersons.")

        deleted_source_logs, _ = SourceRunLog.objects.all().delete()
        self.stdout.write(f"Deleted {deleted_source_logs} SourceRunLogs.")

        deleted_dest_logs, _ = DestinationRunLog.objects.all().delete()
        self.stdout.write(f"Deleted {deleted_dest_logs} DestinationRunLogs.")

        deleted_chats, _ = ChatSession.objects.all().delete()
        self.stdout.write(f"Deleted {deleted_chats} ChatSessions.")

        self.stdout.write(self.style.SUCCESS("Successfully cleaned the database."))
