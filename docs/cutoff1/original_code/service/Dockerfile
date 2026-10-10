# Build
FROM golang:1.22-alpine AS build
WORKDIR /src
COPY go.mod ./
COPY main.go ./
RUN CGO_ENABLED=0 go build -o /sut main.go

# Run — distroless-style scratch image, ~6MB
FROM alpine:3.19
RUN adduser -D -u 10001 sut
COPY --from=build /sut /sut
USER 10001
EXPOSE 8080
ENTRYPOINT ["/sut"]
