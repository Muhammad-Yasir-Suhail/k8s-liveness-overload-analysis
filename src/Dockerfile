# One image, four binaries:  /sut  /loadgen  /ski  /governor
# Build:  docker build -t g18/tools:latest src/
FROM golang:1.22-alpine AS build
WORKDIR /src
COPY go.mod ./
COPY internal ./internal
COPY cmd ./cmd
ENV CGO_ENABLED=0
RUN go build -trimpath -ldflags="-s -w" -o /out/sut      ./cmd/sut \
 && go build -trimpath -ldflags="-s -w" -o /out/loadgen  ./cmd/loadgen \
 && go build -trimpath -ldflags="-s -w" -o /out/ski      ./cmd/ski \
 && go build -trimpath -ldflags="-s -w" -o /out/governor ./cmd/governor

FROM alpine:3.19
RUN adduser -D -u 10001 g18
COPY --from=build /out/ /
USER 10001
EXPOSE 8080
ENTRYPOINT ["/sut"]
